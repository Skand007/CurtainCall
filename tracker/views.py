import csv
import io
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import matching, sources
from .models import Entry, ImportBatch, ImportRow, Title
from .stats import library_stats

log = logging.getLogger(__name__)

BATCH_SIZE = 10  # rows matched per HTMX request, keeps each request well under the host timeout
MAX_UPLOAD_ROWS = 5000
KIND_FILTERS = [
    (Title.KIND_MOVIE, "Films"),
    (Title.KIND_TV, "Series"),
    (Title.KIND_ANIME, "Anime"),
    ("live", "Live-action"),
    ("animated", "Animated"),
]


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def add_to_library(user, title, status=Entry.STATUS_WATCHED):
    entry, _ = Entry.objects.get_or_create(user=user, title=title, defaults={"status": status})
    return entry


def filtered_entries(user, params):
    qs = Entry.objects.filter(user=user).select_related("title")
    kind = params.get("kind", "")
    status = params.get("status", "")
    query = params.get("q", "").strip()
    if kind == "animated":
        qs = qs.filter(Q(title__animated=True) | Q(title__kind=Title.KIND_ANIME))
    elif kind == "live":
        qs = qs.filter(title__animated=False).exclude(title__kind=Title.KIND_ANIME)
    elif kind:
        qs = qs.filter(title__kind=kind)
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


def home(request):
    if request.user.is_authenticated:
        return redirect("library")
    owners = get_user_model().objects.annotate(n=Count("entries")).filter(n__gt=0).order_by("-n")[:5]
    return render(request, "tracker/home.html", {"owners": owners})


@login_required
def library(request):
    page = Paginator(filtered_entries(request.user, request.GET), 60).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "params": request.GET,
        "status_choices": Entry.STATUS_CHOICES,
        "kind_choices": KIND_FILTERS,
        "editable": True,
    }
    template = "tracker/_grid.html" if is_htmx(request) else "tracker/library.html"
    return render(request, template, context)


def profile(request, username):
    owner = get_object_or_404(get_user_model(), username=username)
    page = Paginator(filtered_entries(owner, request.GET), 60).get_page(request.GET.get("page"))
    context = {
        "owner": owner,
        "page": page,
        "params": request.GET,
        "status_choices": Entry.STATUS_CHOICES,
        "kind_choices": KIND_FILTERS,
        "editable": request.user == owner,
        "counts": Entry.objects.filter(user=owner).values("status").annotate(n=Count("id")),
    }
    template = "tracker/_grid.html" if is_htmx(request) else "tracker/profile.html"
    return render(request, template, context)


def stats(request, username):
    owner = get_object_or_404(get_user_model(), username=username)
    return render(request, "tracker/stats.html", {"owner": owner, "stats": library_stats(owner)})


def title_detail(request, pk):
    title = get_object_or_404(Title, pk=pk)
    entry = None
    if request.user.is_authenticated:
        entry = Entry.objects.filter(user=request.user, title=title).first()
    return render(
        request, "tracker/title.html", {"title": title, "entry": entry, "status_choices": Entry.STATUS_CHOICES}
    )


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
    try:
        title = Title.objects.filter(source=source, source_id=source_id).first()
        if title is None:
            title = sources.upsert_title(sources.get_candidate(source, kind, source_id))
        sources.ensure_details(title)
    except (sources.SourceError, OSError) as exc:
        log.warning("add failed: %s", exc)
        return HttpResponse('<span class="muted">Could not reach the service, try again</span>')
    add_to_library(request.user, title, status)
    return HttpResponse(f'<a class="pill ok" href="/title/{title.pk}/">Added as {status}</a>')


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
        return HttpResponse('<span class="muted saved">Saved</span>')
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
                sources.ensure_details(row.matched_title)
                add_to_library(request.user, row.matched_title)
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
            sources.ensure_details(title)
            row.matched_title, row.state, row.candidates = title, ImportRow.MATCHED, []
            row.save()
            add_to_library(request.user, title)
        elif action == "retry":
            row.raw_title = request.POST.get("title", row.raw_title).strip()[:300] or row.raw_title
            year = request.POST.get("year", "").strip()
            row.raw_year = int(year) if year.isdigit() else None
            hint = matching.HINT_ANIMATED if request.POST.get("kind") == "anime" else matching.HINT_MOVIE
            matching.match_row(row, hint)
            if row.state == ImportRow.MATCHED:
                sources.ensure_details(row.matched_title)
                add_to_library(request.user, row.matched_title)
        else:
            return HttpResponseBadRequest("unknown action")
    except (sources.SourceError, OSError, ValueError) as exc:
        log.warning("resolve failed: %s", exc)
        return HttpResponse('<div class="card muted">Could not reach the service, try again.</div>')
    kinds = [(Title.KIND_MOVIE, "Film"), (Title.KIND_TV, "Series"), ("anime", "Anime")]
    return render(request, "tracker/_review_row.html", {"row": row, "kinds": kinds})
