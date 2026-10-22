import csv
import hmac
import io
import logging
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import dedup, matching, sources
from .models import Entry, ImportBatch, ImportRow, Title
from .stats import library_stats

log = logging.getLogger(__name__)

BATCH_SIZE = 10  # rows matched per HTMX request, keeps each request well under the host timeout
MAX_UPLOAD_ROWS = 5000
KIND_FILTERS = [
    ("", "All"),
    (Title.KIND_MOVIE, "Films"),
    (Title.KIND_TV, "Series"),
    (Title.KIND_ANIME, "Anime"),
]
STYLE_FILTERS = [("live", "Live-action"), ("animated", "Animated")]


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def add_to_library(user, title, status=Entry.STATUS_WATCHED):
    entry, _ = Entry.objects.get_or_create(user=user, title=title, defaults={"status": status})
    return entry


def filtered_entries(user, params):
    qs = Entry.objects.filter(user=user).select_related("title")
    kind = params.get("kind", "")
    style = params.get("style", "")
    status = params.get("status", "")
    query = params.get("q", "").strip()
    if kind in ("live", "animated"):  # older links used kind=live / kind=animated
        style, kind = kind, ""
    if kind:
        qs = qs.filter(title__kind=kind)
    if style == "animated":
        qs = qs.filter(Q(title__animated=True) | Q(title__kind=Title.KIND_ANIME))
    elif style == "live":
        qs = qs.filter(title__animated=False).exclude(title__kind=Title.KIND_ANIME)
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(Q(title__name__icontains=query) | Q(title__original_name__icontains=query))
    sort = params.get("sort", "recent")
    order = {
        "recent": ["-updated_at"],
        "name": ["title__name"],
        "year": ["-title__year", "title__name"],
        "rating": ["-rating", "title__name"],
    }.get(sort, ["-updated_at"])
    return qs.order_by(*order)


# ---------------------------------------------------------------- pages


def get_owner():
    """The person whose library the site shows: OWNER_USERNAME if set, else the first superuser."""
    users = get_user_model().objects
    if settings.OWNER_USERNAME:
        return users.filter(username=settings.OWNER_USERNAME).first()
    return users.filter(is_superuser=True).order_by("id").first()


def display_name(user):
    return user.get_full_name() or user.username


def library_page(request, owner):
    page = Paginator(filtered_entries(owner, request.GET), 60).get_page(request.GET.get("page"))
    watched = Entry.objects.filter(user=owner, status=Entry.STATUS_WATCHED)
    minutes = watched.aggregate(m=Sum("title__runtime_minutes"))["m"] or 0
    context = {
        "owner": owner,
        "owner_name": display_name(owner),
        "page": page,
        "params": request.GET,
        "status_choices": Entry.STATUS_CHOICES,
        "kind_choices": KIND_FILTERS,
        "style_choices": STYLE_FILTERS,
        "editable": request.user == owner,
        "watched_count": watched.count(),
        "hours": round(minutes / 60),
    }
    if is_htmx(request):
        return render(request, "tracker/_grid.html", context)

    with_poster = watched.exclude(title__poster_url="").select_related("title")
    recent = list(with_poster.order_by(F("watched_on").desc(nulls_last=True), "-created_at")[:14])
    # The banner uses the latest watched title that has a wide image.
    banner = (
        watched.exclude(title__backdrop_url="")
        .select_related("title")
        .order_by(F("watched_on").desc(nulls_last=True), "-created_at")
        .first()
    )
    context.update(
        banner=banner,
        recent=recent,
        top_rated=list(with_poster.filter(rating__gte=4).order_by("-rating", "-watched_on", "title__name")[:14]),
    )
    return render(request, "tracker/library.html", context)


def home(request):
    owner = get_owner()
    if owner is None:
        return render(request, "tracker/home.html")
    return library_page(request, owner)


def library(request):
    # Old links pointed here; everyone now lands on the public library.
    if not request.user.is_authenticated:
        query = request.GET.urlencode()
        return redirect(reverse("home") + ("?" + query if query else ""))
    return library_page(request, request.user)


def profile(request, username):
    return library_page(request, get_object_or_404(get_user_model(), username=username))


def stats(request, username):
    owner = get_object_or_404(get_user_model(), username=username)
    data = library_stats(owner)
    context = {
        "owner": owner,
        "owner_name": display_name(owner),
        "stats": data,
        "decade_rows": list(zip(data["decades"]["labels"], data["decades"]["values"])),
        "score_rows": list(zip(data["scores"]["labels"], data["scores"]["critics"], data["scores"]["audience"])),
    }
    return render(request, "tracker/stats.html", context)


def title_detail(request, pk):
    title = get_object_or_404(Title, pk=pk)
    owner = get_owner()
    viewer_entry = None
    if request.user.is_authenticated:
        viewer_entry = Entry.objects.filter(user=request.user, title=title).first()
    # Visitors see the site owner's take on the title, read-only. Private notes are never shown.
    public_entry = None
    if owner is not None and request.user != owner:
        public_entry = Entry.objects.filter(user=owner, title=title).first()
    context = {
        "title": title,
        "entry": viewer_entry,
        "public_entry": public_entry,
        "owner_name": display_name(owner) if owner else "",
        "status_choices": Entry.STATUS_CHOICES,
        "rating_choices": [(x / 2, x) for x in range(1, 11)],
    }
    return render(request, "tracker/title.html", context)


@login_required
def change_password(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)  # stay logged in after the change
        messages.success(request, "Password changed.")
        return redirect("home")
    return render(request, "tracker/password.html", {"form": form})


# ---------------------------------------------------------------- search and entries


@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    source = request.GET.get("source", "movie")
    results, error = [], ""
    if query:
        try:
            if source == "anime":
                results = sources.search_anilist(query, limit=12)
            elif source == "tv":
                results = sources.search_tmdb(query, Title.KIND_TV, limit=12)
            else:
                results = sources.search_tmdb(query, Title.KIND_MOVIE, limit=12)
        except (sources.SourceError, OSError) as exc:
            log.warning("search failed: %s", exc)
            error = "The search service didn't respond. Try again in a moment."
        owned = set(
            Entry.objects.filter(user=request.user, title__source__in=[r["source"] for r in results]).values_list(
                "title__source", "title__source_id"
            )
        )
        for r in results:
            r["owned"] = (r["source"], r["source_id"]) in owned
    context = {"query": query, "source": source, "results": results, "error": error}
    template = "tracker/_search_results.html" if is_htmx(request) else "tracker/search.html"
    return render(request, template, context)


@login_required
@require_POST
def add_title(request):
    source = request.POST.get("source")
    kind = request.POST.get("kind")
    source_id = request.POST.get("source_id", "")
    status = request.POST.get("status", Entry.STATUS_WATCHED)
    if source not in (Title.SOURCE_TMDB, Title.SOURCE_ANILIST) or not source_id.isdigit():
        return HttpResponseBadRequest("bad title reference")
    if status not in dict(Entry.STATUS_CHOICES):
        status = Entry.STATUS_WATCHED
    use_title = request.POST.get("use_title", "")
    try:
        if use_title.isdigit():
            # The person confirmed it is the same work as a title already in the library.
            title = get_object_or_404(Title, pk=int(use_title), entries__user=request.user)
        else:
            title = Title.objects.filter(source=source, source_id=source_id).first()
            if title is None:
                candidate = sources.get_candidate(source, kind, source_id)
                twin = None if request.POST.get("confirm") else dedup.duplicate_in_library(request.user, candidate)
                if twin is not None:
                    context = {"twin": twin, "source": source, "kind": kind, "source_id": source_id, "status": status}
                    return render(request, "tracker/_duplicate_prompt.html", context)
                title = sources.upsert_title(candidate)
            title = sources.ensure_details(title)
    except (sources.SourceError, OSError) as exc:
        log.warning("add failed: %s", exc)
        return HttpResponse('<span class="muted">Could not reach the service. Try again in a moment.</span>')
    fetch_scores_quietly(title)
    add_to_library(request.user, title, status)
    label = dict(Entry.STATUS_CHOICES)[status]
    return HttpResponse(f'<a class="added" href="/title/{title.pk}/">Added to {label}</a>')


def fetch_scores_quietly(title):
    """IMDb and Rotten Tomatoes scores are a bonus: never let them block adding a title."""
    if settings.OMDB_API_KEY and title.imdb_id and title.scores_checked_at is None:
        try:
            sources.fetch_omdb_scores(title)
        except (sources.SourceError, OSError, ValueError) as exc:
            log.info("score lookup skipped for %s: %s", title, exc)


def settle_match(row, user, include_anilist=True):
    """After a row matches: fetch details, fold into an existing twin, add to the library."""
    title = sources.ensure_details(row.matched_title, include_anilist=include_anilist)
    title = dedup.fold_import_duplicate(title, user)
    if row.matched_title_id != title.pk:
        row.matched_title = title
        row.save()
    add_to_library(user, title)


@login_required
@require_POST
def update_entry(request, pk):
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    status = request.POST.get("status")
    if status in dict(Entry.STATUS_CHOICES):
        entry.status = status
    if "rating" in request.POST:
        raw = request.POST.get("rating", "")
        if raw == "":
            entry.rating = None
        else:
            try:
                value = round(float(raw) * 2) / 2
            except ValueError:
                return HttpResponseBadRequest("bad rating")
            if not 0.5 <= value <= 5:
                return HttpResponseBadRequest("rating must be 0.5 to 5")
            entry.rating = value
    if "watched_on" in request.POST:
        entry.watched_on = request.POST.get("watched_on") or None
    if "notes" in request.POST:
        entry.notes = request.POST.get("notes", "")[:5000]
    entry.save()
    if is_htmx(request):
        return HttpResponse('<span class="saved" role="status">Saved</span>')
    return redirect("title", pk=entry.title_id)


@login_required
@require_POST
def delete_entry(request, pk):
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    title_pk = entry.title_id
    entry.delete()
    # Keep import history consistent: a removed title is no longer a live match.
    ImportRow.objects.filter(batch__user=request.user, matched_title_id=title_pk, state=ImportRow.MATCHED).update(
        state=ImportRow.SKIPPED
    )
    messages.info(request, "Removed from your library.")
    return redirect("title", pk=title_pk)


# ---------------------------------------------------------------- CSV import


def read_csv_rows(uploaded):
    text = uploaded.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip().lower(): f for f in (reader.fieldnames or [])}
    title_col = fields.get("title") or fields.get("name")
    year_col = fields.get("year")
    if not title_col:
        raise ValueError("The CSV needs a 'Title' column.")
    rows = []
    for record in reader:
        name = (record.get(title_col) or "").strip()
        if not name:
            continue
        year = (record.get(year_col) or "").strip() if year_col else ""
        rows.append((name[:300], int(year) if year.isdigit() else None))
        if len(rows) > MAX_UPLOAD_ROWS:
            raise ValueError(f"Files are limited to {MAX_UPLOAD_ROWS} rows.")
    return rows


@login_required
def import_list(request):
    if request.method == "POST":
        uploaded = request.FILES.get("file")
        hint = request.POST.get("hint", matching.HINT_MOVIE)
        if hint not in (matching.HINT_MOVIE, matching.HINT_ANIMATED):
            hint = matching.HINT_MOVIE
        if not uploaded:
            messages.error(request, "Choose a CSV file first.")
            return redirect("import_list")
        try:
            rows = read_csv_rows(uploaded)
        except (ValueError, UnicodeDecodeError) as exc:
            messages.error(request, f"Couldn't read that file: {exc}")
            return redirect("import_list")
        batch = ImportBatch.objects.create(user=request.user, filename=uploaded.name[:255], hint=hint)
        ImportRow.objects.bulk_create(ImportRow(batch=batch, raw_title=t, raw_year=y) for t, y in rows)
        return redirect("import_detail", pk=batch.pk)

    batches = ImportBatch.objects.filter(user=request.user).annotate(
        total=Count("rows"),
        pending=Count("rows", filter=Q(rows__state=ImportRow.PENDING)),
        review=Count("rows", filter=Q(rows__state__in=[ImportRow.REVIEW, ImportRow.NOT_FOUND])),
    )
    return render(request, "tracker/import_list.html", {"batches": batches})


def batch_progress(batch):
    counts = dict(batch.rows.values_list("state").annotate(n=Count("id")))
    total = sum(counts.values())
    pending = counts.get(ImportRow.PENDING, 0)
    return {
        "total": total,
        "pending": pending,
        "done": total - pending,
        "percent": round(100 * (total - pending) / total) if total else 100,
        "counts": counts,
    }


@login_required
def import_detail(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk, user=request.user)
    review_rows = batch.rows.filter(state__in=[ImportRow.REVIEW, ImportRow.NOT_FOUND])
    context = {
        "batch": batch,
        "progress": batch_progress(batch),
        "review_rows": review_rows,
        "kinds": [(Title.KIND_MOVIE, "Film"), (Title.KIND_TV, "Series"), ("anime", "Anime")],
    }
    return render(request, "tracker/import_detail.html", context)


@login_required
@require_POST
def import_process(request, pk):
    """Match the next few pending rows. The returned fragment re-triggers itself until done."""
    batch = get_object_or_404(ImportBatch, pk=pk, user=request.user)
    error = ""
    for row in batch.rows.filter(state=ImportRow.PENDING)[:BATCH_SIZE]:
        try:
            matching.match_row(row, batch.hint)
            if row.state == ImportRow.MATCHED:
                # AniList details wait for the nightly refresh, to stay under its rate limit.
                settle_match(row, request.user, include_anilist=False)
        except (sources.SourceError, OSError) as exc:
            log.warning("import row %s failed: %s", row.pk, exc)
            error = str(exc)
            break
    progress = batch_progress(batch)
    response = render(request, "tracker/_import_progress.html", {"batch": batch, "progress": progress, "error": error})
    if not progress["pending"]:
        response["HX-Refresh"] = "true"  # reload the page so the review list appears
    return response


@login_required
@require_POST
def import_resolve(request, row_pk):
    row = get_object_or_404(ImportRow, pk=row_pk, batch__user=request.user)
    action = request.POST.get("action")
    try:
        if action == "skip":
            row.state = ImportRow.SKIPPED
            row.save()
        elif action == "pick":
            index = int(request.POST.get("choice", "-1"))
            if not 0 <= index < len(row.candidates):
                return HttpResponseBadRequest("pick a candidate")
            title = sources.upsert_title(row.candidates[index])
            row.matched_title, row.state, row.candidates = title, ImportRow.MATCHED, []
            row.save()
            settle_match(row, request.user)
        elif action == "retry":
            row.raw_title = request.POST.get("title", row.raw_title).strip()[:300] or row.raw_title
            year = request.POST.get("year", "").strip()
            row.raw_year = int(year) if year.isdigit() else None
            hint = matching.HINT_ANIMATED if request.POST.get("kind") == "anime" else matching.HINT_MOVIE
            matching.match_row(row, hint)
            if row.state == ImportRow.MATCHED:
                settle_match(row, request.user)
        else:
            return HttpResponseBadRequest("unknown action")
    except (sources.SourceError, OSError, ValueError) as exc:
        log.warning("resolve failed: %s", exc)
        return HttpResponse('<div class="card muted">Could not reach the service, try again.</div>')
    kinds = [(Title.KIND_MOVIE, "Film"), (Title.KIND_TV, "Series"), ("anime", "Anime")]
    return render(request, "tracker/_review_row.html", {"row": row, "kinds": kinds})


# ---------------------------------------------------------------- duplicates


@login_required
def duplicates(request):
    return render(request, "tracker/duplicates.html", {"pairs": dedup.likely_pairs(request.user)})


@login_required
@require_POST
def merge_duplicate(request):
    library = Title.objects.filter(entries__user=request.user).distinct()
    keep = get_object_or_404(library, pk=request.POST.get("keep", "0"))
    drop = get_object_or_404(library, pk=request.POST.get("drop", "0"))
    dedup.merge_titles(keep, drop)
    messages.success(request, f"Merged into {keep}.")
    return redirect("duplicates")


# ---------------------------------------------------------------- nightly refresh

REFRESH_SECONDS = 20  # stay well inside the web server's 60-second request timeout


@csrf_exempt
@require_POST
def refresh_batch(request):
    """Fill in missing details and scores for a few titles. Called repeatedly by GitHub Actions.

    Protected by a shared token. Returns how much work is left so the caller knows when to stop.
    """
    token = request.headers.get("X-Refresh-Token", "")
    if not settings.REFRESH_TOKEN or not hmac.compare_digest(token, settings.REFRESH_TOKEN):
        return JsonResponse({"error": "not allowed"}, status=403)
    try:
        omdb_budget = max(0, int(request.POST.get("omdb_budget", "0")))
    except ValueError:
        omdb_budget = 0

    started = time.monotonic()
    details_done = scores_done = 0
    stopped = ""
    needs_details = Title.objects.filter(details_fetched=False).order_by("id")
    for title in needs_details[:200]:
        if time.monotonic() - started > REFRESH_SECONDS:
            stopped = "time"
            break
        try:
            sources.ensure_details(title)
            details_done += 1
        except sources.RateLimited as exc:
            stopped = str(exc)
            break
        except (sources.SourceError, OSError, ValueError) as exc:
            log.warning("refresh details failed for %s: %s", title.pk, exc)
            Title.objects.filter(pk=title.pk).update(details_fetched=True)  # do not retry forever
        sources.pause_for(title)

    needs_scores = Title.objects.exclude(imdb_id="").filter(scores_checked_at__isnull=True).order_by("id")
    if not stopped and settings.OMDB_API_KEY:
        for title in needs_scores[:omdb_budget]:
            if time.monotonic() - started > REFRESH_SECONDS:
                stopped = "time"
                break
            try:
                sources.fetch_omdb_scores(title)
                scores_done += 1
            except sources.RateLimited as exc:
                stopped = str(exc)
                break
            except (sources.SourceError, OSError, ValueError) as exc:
                log.warning("refresh scores failed for %s: %s", title.pk, exc)
                Title.objects.filter(pk=title.pk).update(scores_checked_at=timezone.now())

    return JsonResponse(
        {
            "details_done": details_done,
            "scores_done": scores_done,
            "details_left": needs_details.count(),
            "scores_left": needs_scores.count() if settings.OMDB_API_KEY else 0,
            "stopped": stopped,
        }
    )
