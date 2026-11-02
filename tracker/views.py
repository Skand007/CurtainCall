import csv
import hmac
import io
import logging
import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import dedup, matching, sources
from .models import Entry, ImportBatch, ImportRow, Title
from .stats import SCORE_BANDS, audience_score, band_of, library_stats
from .templatetags.tracker_extras import stars

log = logging.getLogger(__name__)

BATCH_SIZE = 10  # at most this many rows per HTMX request...
BATCH_SECONDS = 15  # ...and stop early when MyAnimeList's pacing makes a batch slow
MAX_UPLOAD_ROWS = 5000
KIND_FILTERS = [("", "All"), (Title.KIND_MOVIE, "Films"), (Title.KIND_TV, "Series")]
STYLE_FILTERS = [("live", "Live action"), ("animated", "Animated")]
FACETS = ["decade", "year", "genre", "director", "studio", "critics", "audience", "source", "runtime"]
SEARCH_SOURCES = [("movie", "Films"), ("tv", "Series"), ("mal", "MyAnimeList")]
RETRY_KINDS = [(Title.KIND_MOVIE, "Film (TMDB)"), (Title.KIND_TV, "Series (TMDB)"), ("mal", "Animation (MyAnimeList)")]


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def add_to_library(user, title, status=Entry.STATUS_WATCHED):
    """Returns (entry, created)."""
    return Entry.objects.get_or_create(user=user, title=title, defaults={"status": status})


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def filtered_entries(user, params):
    qs = Entry.objects.filter(user=user).select_related("title")
    kind = params.get("kind", "")
    style = params.get("style", "")
    status = params.get("status", "")
    query = params.get("q", "").strip()
    if kind in ("live", "animated"):  # older links used kind=live / kind=animated
        style, kind = kind, ""
    if kind == "anime":  # older links: anime is animated now
        style, kind = "animated", ""
    if kind in (Title.KIND_MOVIE, Title.KIND_TV):
        qs = qs.filter(title__kind=kind)
    if style == "animated":
        qs = qs.filter(title__animated=True)
    elif style == "live":
        qs = qs.filter(title__animated=False)
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(Q(title__name__icontains=query) | Q(title__original_name__icontains=query))

    decade, year = as_int(params.get("decade")), as_int(params.get("year"))
    if decade is not None:
        qs = qs.filter(title__year__gte=decade, title__year__lte=decade + 9)
    if year is not None:
        qs = qs.filter(title__year=year)
    critics = as_int(params.get("critics"))
    if critics is not None:
        qs = qs.filter(title__rt_critics__gte=critics, title__rt_critics__lte=100 if critics >= 90 else critics + 9)
    if params.get("runtime") == "missing":
        qs = qs.filter(Q(title__runtime_minutes__isnull=True) | Q(title__runtime_minutes=0))
    if params.get("source") in dict(Title.SOURCE_CHOICES):
        qs = qs.filter(title__source=params["source"])
    # List fields (genres, directors, studios) and the rounded audience score are matched in Python,
    # which works the same on SQLite and Postgres and is quick for one person's library.
    genre, director, studio = params.get("genre"), params.get("director"), params.get("studio")
    audience = as_int(params.get("audience"))
    if genre or director or studio or audience is not None:
        keep = []
        for e in qs.only("id", "title__genres", "title__directors", "title__studios", "title__imdb_rating"):
            t = e.title
            if genre and genre not in t.genres:
                continue
            if director and director not in t.directors:
                continue
            if studio and studio not in t.studios:
                continue
            if audience is not None and (audience_score(t) is None or band_of(audience_score(t)) != audience):
                continue
            keep.append(e.pk)
        qs = qs.filter(pk__in=keep)

    sort = params.get("sort", "recent")
    order = {
        "recent": ["-updated_at"],
        "name": ["title__name"],
        "year": ["-title__year", "title__name"],
        "rating": ["-rating", "title__name"],
    }.get(sort, ["-updated_at"])
    return qs.order_by(*order)


def facet_label(name, value):
    band = as_int(value)
    if name == "decade":
        return f"{value}s"
    if name in ("critics", "audience") and band is not None:
        label = SCORE_BANDS[min(band // 10, 9)]
        return f"Critics {label}%" if name == "critics" else f"Audience {label}"
    if name == "source":
        return dict(Title.SOURCE_CHOICES).get(value, value)
    if name == "runtime":
        return "No runtime yet"
    return f"{name.title()}: {value}"


def facet_chips(request):
    """Active click-through filters, each with a link that removes it."""
    chips = []
    for name in FACETS:
        value = request.GET.get(name)
        if value:
            rest = {k: v for k, v in request.GET.items() if k not in (name, "page")}
            chips.append({"label": facet_label(name, value), "clear": "?" + urlencode(rest)})
    return chips


def library_link(owner, **params):
    """A link into the owner's library with click-through filters, e.g. library_link(owner, genre="Drama")."""
    site_owner = get_owner()
    base = reverse("home") if site_owner and owner.pk == site_owner.pk else reverse("profile", args=[owner.username])
    return base + "?" + urlencode(params)


# ---------------------------------------------------------------- pages


def get_owner():
    """The person whose library the site shows: OWNER_USERNAME if set, else the first superuser."""
    users = get_user_model().objects
    if settings.OWNER_USERNAME:
        return users.filter(username=settings.OWNER_USERNAME).first()
    return users.filter(is_superuser=True).order_by("id").first()


def display_name(user):
    """'SKAND GUPTA' or 'skand' -> 'Skand Gupta' / 'Skand'. Mixed-case names are left as typed."""
    name = (user.get_full_name() or user.username).strip()
    if name.isupper() or name.islower():
        name = name.title()
    return name


def library_page(request, owner):
    page = Paginator(filtered_entries(owner, request.GET), 60).get_page(request.GET.get("page"))
    watched = Entry.objects.filter(user=owner, status=Entry.STATUS_WATCHED)
    minutes = watched.aggregate(m=Sum("title__runtime_minutes"))["m"] or 0
    editable = request.user == owner
    context = {
        "owner": owner,
        "owner_name": display_name(owner),
        "page": page,
        "params": request.GET,
        "status_choices": Entry.STATUS_CHOICES,
        "kind_choices": KIND_FILTERS,
        "style_choices": STYLE_FILTERS,
        "editable": editable,
        "selecting": editable and request.GET.get("select") == "1",
        "chips": facet_chips(request),
        "hidden_facets": [(k, request.GET[k]) for k in FACETS if k != "genre" and request.GET.get(k)],
        "watched_count": watched.count(),
        "hours": round(minutes / 60),
    }
    if is_htmx(request):
        return render(request, "tracker/_grid.html", context)
    count, hours = context["watched_count"], context["hours"]
    context["og_description"] = (
        f"{context['owner_name']} has watched {count:,} film{'s' if count != 1 else ''} and series, "
        f"about {hours:,} hours in all. Browse the library, ratings and stats."
    )

    genres = sorted(
        {g for gs in Title.objects.filter(entries__user=owner).values_list("genres", flat=True) for g in gs}
    )
    with_poster = watched.exclude(title__poster_url="").select_related("title")
    recent = list(with_poster.order_by(F("watched_on").desc(nulls_last=True), "-created_at")[:14])
    context.update(
        genre_choices=genres,
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
    decades = data["decades"]
    scores = data["scores"]
    link = lambda **kw: library_link(owner, **kw)  # noqa: E731
    context = {
        "owner": owner,
        "owner_name": display_name(owner),
        "stats": data,
        "decade_rows": [
            (label, value, link(decade=key))
            for label, value, key in zip(decades["labels"], decades["values"], decades["keys"], strict=True)
        ],
        "score_rows": [
            (label, c, a, link(critics=key), link(audience=key))
            for label, c, a, key in zip(
                scores["labels"], scores["critics"], scores["audience"], scores["keys"], strict=True
            )
        ],
        "genre_rows": [(name, n, link(genre=name)) for name, n in data["genres"]],
        "director_rows": [(name, n, link(director=name)) for name, n in data["directors"]],
        "studio_rows": [(name, n, link(studio=name)) for name, n in data["studios"]],
        "og_title": f"{display_name(owner)}'s stats · CurtainCall",
        "og_description": (
            f"{data['total']:,} titles watched, about {data['hours']:,} hours: {data['live_action']:,} live action "
            f"and {data['animated']:,} animated, by decade, genre, director, studio and score."
        ),
        "links": {
            "all": link(status="watched"),
            "animated": link(style="animated", status="watched"),
            "live": link(style="live", status="watched"),
            "no_runtime": link(runtime="missing", status="watched"),
            "decade": link(decade="DECADE"),
            "critics": link(critics="BAND"),
            "audience": link(audience="BAND"),
        },
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
    is_owner = owner is not None and request.user == owner
    link_owner = owner or (request.user if request.user.is_authenticated else None)
    # Link preview: the owner's rating (if any) and the start of the overview, with the poster.
    overview = " ".join((title.overview or "").split())
    if len(overview) > 220:
        overview = overview[:220].rsplit(" ", 1)[0] + "…"
    owner_entry = Entry.objects.filter(user=owner, title=title).first() if owner else None
    rated = (
        f"{display_name(owner)} rated it {stars(owner_entry.rating)}. " if owner_entry and owner_entry.rating else ""
    )
    preview = {"og_title": f"{title} · CurtainCall", "og_description": (rated + overview).strip() or f"{title}."}
    if title.poster_url:
        preview.update(og_image=title.poster_url, og_image_is_card=False)
    context = {
        **preview,
        "title": title,
        "entry": viewer_entry,
        "public_entry": public_entry,
        "owner_name": display_name(owner) if owner else "",
        "status_choices": Entry.STATUS_CHOICES,
        "rating_choices": [(x / 2, x) for x in range(1, 11)],
        "can_edit": is_owner,
        "kind_choices": Title.KIND_CHOICES,
        "genre_links": [(g, library_link(link_owner, genre=g)) for g in title.genres] if link_owner else [],
        "studio_links": [(s, library_link(link_owner, studio=s)) for s in title.studios] if link_owner else [],
        "director_links": [(d, library_link(link_owner, director=d)) for d in title.directors] if link_owner else [],
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


# ---------------------------------------------------------------- owner edits


def require_owner(request):
    owner = get_owner()
    if owner is None or request.user != owner:
        raise Http404("Only the owner can change titles.")
    return owner


def apply_edit(title, kind=None, animated=None, studios=None):
    """Set fields by hand and lock them so refreshes keep the owner's choice."""
    locked = list(title.locked or [])
    if kind in (Title.KIND_MOVIE, Title.KIND_TV):
        title.kind = kind
        locked.append("kind")
    if animated is not None:
        title.animated = animated
        locked.append("animated")
    if studios is not None:
        title.studios = studios
        locked.append("studios")
    title.locked = sorted(set(locked))
    title.save()


@require_POST
def edit_title(request, pk):
    require_owner(request)
    title = get_object_or_404(Title, pk=pk)
    # Only what actually changed is set (and locked), so saving the form unchanged locks nothing.
    kind = request.POST.get("kind")
    animated = {"animated": True, "live": False}.get(request.POST.get("style"))
    raw_studios = request.POST.get("studios")
    studios = [s.strip() for s in raw_studios.split(",") if s.strip()] if raw_studios is not None else None
    apply_edit(
        title,
        kind=kind if kind != title.kind else None,
        animated=animated if animated is not None and animated != title.animated else None,
        studios=studios if studios is not None and studios != title.studios else None,
    )
    messages.success(request, "Saved. Refreshes will keep what you changed.")
    return redirect("title", pk=title.pk)


@require_POST
def bulk_edit(request):
    owner = require_owner(request)
    ids = [int(x) for x in request.POST.getlist("title") if x.isdigit()]
    titles = Title.objects.filter(pk__in=ids, entries__user=owner).distinct()
    kind = request.POST.get("kind")
    animated = {"animated": True, "live": False}.get(request.POST.get("style"))
    if kind not in (Title.KIND_MOVIE, Title.KIND_TV) and animated is None:
        messages.error(request, "Choose a type or a style to change.")
    else:
        for title in titles:
            apply_edit(title, kind=kind, animated=animated)
        messages.success(request, f"Updated {len(titles)} title{'s' if len(titles) != 1 else ''}.")
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = reverse("home")
    return redirect(back)


def search_everywhere(query):
    """MyAnimeList, TMDB films and TMDB series together, for re-matching a title."""
    results, errors = [], []
    for search in (
        lambda: sources.search_mal(query, limit=6),
        lambda: sources.search_tmdb(query, Title.KIND_MOVIE, limit=6),
        lambda: sources.search_tmdb(query, Title.KIND_TV, limit=6),
    ):
        try:
            results += search()
        except (sources.SourceError, OSError) as exc:
            errors.append(str(exc))
    return results, errors


def rematch(request, pk):
    """Swap a wrongly matched title for the right one, keeping the owner's rating, dates and notes."""
    require_owner(request)
    title = get_object_or_404(Title, pk=pk)
    if request.method == "POST":
        source, kind, source_id = (
            request.POST.get("source"),
            request.POST.get("kind"),
            request.POST.get("source_id", ""),
        )
        if source not in (Title.SOURCE_TMDB, Title.SOURCE_MAL) or not source_id.isdigit():
            return HttpResponseBadRequest("bad title reference")
        try:
            new = Title.objects.filter(source=source, source_id=source_id).first()
            if new is None:
                new = sources.upsert_title(sources.get_candidate(source, kind, source_id))
            new = sources.ensure_details(new)
        except (sources.SourceError, OSError) as exc:
            log.warning("rematch failed: %s", exc)
            messages.error(request, "Could not reach the service. Try again in a moment.")
            return redirect("rematch", pk=title.pk)
        if new.pk != title.pk:
            dedup.move_entries(title, new)
            if not title.entries.exists():
                title.delete()
        fetch_scores_quietly(new)
        messages.success(request, f"Now matched to {new}.")
        return redirect("title", pk=new.pk)
    query = request.GET.get("q", title.name).strip()
    results, errors = search_everywhere(query) if query else ([], [])
    results = [r for r in results if not (r["source"] == title.source and r["source_id"] == title.source_id)]
    context = {"title": title, "query": query, "results": results, "error": bool(errors) and not results}
    return render(request, "tracker/rematch.html", context)


# ---------------------------------------------------------------- search and entries


@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    source = request.GET.get("source", "movie")
    if source == "anime":  # older links
        source = "mal"
    results, error = [], ""
    if query:
        try:
            if source == "mal":
                results = sources.search_mal(query, limit=12)
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
            r["ref"] = f"{r['source']}|{r['kind']}|{r['source_id']}"
    context = {"query": query, "source": source, "sources": SEARCH_SOURCES, "results": results, "error": error}
    template = "tracker/_search_results.html" if is_htmx(request) else "tracker/search.html"
    return render(request, template, context)


def parse_ref(ref):
    """'tmdb|movie|949' -> ('tmdb', 'movie', '949'), or None if it is not a valid reference."""
    parts = (ref or "").split("|")
    if len(parts) != 3 or parts[0] not in (Title.SOURCE_TMDB, Title.SOURCE_MAL) or not parts[2].isdigit():
        return None
    return tuple(parts)


def title_for(user, source, kind, source_id, check_twin=True):
    """Find or create the Title for a search result. Returns (title, twin): when the library already holds
    the same work from another source, title is None and twin is that title."""
    title = Title.objects.filter(source=source, source_id=source_id).first()
    if title is None:
        candidate = sources.get_candidate(source, kind, source_id)
        twin = dedup.duplicate_in_library(user, candidate) if check_twin else None
        if twin is not None:
            return None, twin
        title = sources.upsert_title(candidate)
    return sources.ensure_details(title), None


def added_fragment(request, entry, created, status_label):
    return render(request, "tracker/_added.html", {"entry": entry, "created": created, "label": status_label})


@login_required
@require_POST
def add_title(request):
    source = request.POST.get("source")
    kind = request.POST.get("kind")
    source_id = request.POST.get("source_id", "")
    status = request.POST.get("status", Entry.STATUS_WATCHED)
    if source not in (Title.SOURCE_TMDB, Title.SOURCE_MAL, Title.SOURCE_ANILIST) or not source_id.isdigit():
        return HttpResponseBadRequest("bad title reference")
    if status not in dict(Entry.STATUS_CHOICES):
        status = Entry.STATUS_WATCHED
    use_title = request.POST.get("use_title", "")
    try:
        if use_title.isdigit():
            # The person confirmed it is the same work as a title already in the library.
            title = get_object_or_404(Title, pk=int(use_title), entries__user=request.user)
        elif source == Title.SOURCE_ANILIST:
            title = get_object_or_404(Title, source=source, source_id=source_id)  # old titles: no new lookups
        else:
            title, twin = title_for(request.user, source, kind, source_id, check_twin=not request.POST.get("confirm"))
            if twin is not None:
                context = {"twin": twin, "source": source, "kind": kind, "source_id": source_id, "status": status}
                return render(request, "tracker/_duplicate_prompt.html", context)
    except (sources.SourceError, OSError) as exc:
        log.warning("add failed: %s", exc)
        return HttpResponse('<span class="muted">Could not reach the service. Try again in a moment.</span>')
    fetch_scores_quietly(title)
    entry, created = add_to_library(request.user, title, status)
    return added_fragment(request, entry, created, dict(Entry.STATUS_CHOICES)[status])


@login_required
@require_POST
def add_many(request):
    """Add every ticked search result. A result that is already in the library under another source is
    not added twice; the summary says so instead."""
    status = request.POST.get("status", Entry.STATUS_WATCHED)
    if status not in dict(Entry.STATUS_CHOICES):
        status = Entry.STATUS_WATCHED
    outcomes = []
    for ref in request.POST.getlist("pick"):
        parsed = parse_ref(ref)
        if parsed is None:
            continue
        try:
            title, twin = title_for(request.user, *parsed)
        except (sources.SourceError, OSError) as exc:
            log.warning("add failed: %s", exc)
            outcomes.append({"error": True, "ref": ref})
            continue
        if twin is not None:
            outcomes.append({"twin": twin})
            continue
        fetch_scores_quietly(title)
        entry, created = add_to_library(request.user, title, status)
        outcomes.append({"entry": entry, "created": created})
    label = dict(Entry.STATUS_CHOICES)[status]
    return render(request, "tracker/_added_many.html", {"outcomes": outcomes, "label": label})


@login_required
@require_POST
def undo_add(request, pk):
    """Take back an add. Only entries with nothing of the owner's own in them yet can be undone this way."""
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    if entry.rating is not None or entry.notes:
        return HttpResponse(
            '<span class="muted">This one has your rating or notes now; remove it from its page.</span>'
        )
    title = entry.title
    entry.delete()
    ImportRow.objects.filter(batch__user=request.user, matched_title=title, state=ImportRow.MATCHED).update(
        state=ImportRow.SKIPPED
    )
    ref = {"source": title.source, "kind": title.tmdb_type or title.kind, "source_id": title.source_id}
    return render(request, "tracker/_add_form.html", {"r": ref, "undone": True})


def fetch_scores_quietly(title):
    """IMDb and Rotten Tomatoes scores are a bonus: never let them block adding a title."""
    if settings.OMDB_API_KEY and title.imdb_id and title.scores_checked_at is None:
        try:
            sources.fetch_omdb_scores(title)
        except (sources.SourceError, OSError, ValueError) as exc:
            log.info("score lookup skipped for %s: %s", title, exc)


def settle_title(row, user, title, include_slow=True):
    """Fetch details for one matched title, fold it into an existing twin, add it to the library and
    remember on the row what was added, so the choice can be undone."""
    title = sources.ensure_details(title, include_slow=include_slow)
    title = dedup.fold_import_duplicate(title, user)
    entry, created = add_to_library(user, title)
    if not row.picked_title_ids:
        row.matched_title = title
    row.picked_title_ids = list(dict.fromkeys([*row.picked_title_ids, title.pk]))
    if created:
        row.created_entry_ids = [*row.created_entry_ids, entry.pk]
    row.state = ImportRow.MATCHED
    row.save()
    return title


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
    settled_rows = batch.rows.filter(state__in=[ImportRow.MATCHED, ImportRow.SKIPPED]).select_related("matched_title")
    context = {
        "batch": batch,
        "progress": batch_progress(batch),
        "review_rows": review_rows,
        "settled_rows": settled_rows,
        "kinds": RETRY_KINDS,
    }
    return render(request, "tracker/import_detail.html", context)


BACKUP_COLUMNS = [
    "Title",
    "Year",
    "Type",
    "Style",
    "Status",
    "Rating",
    "WatchedDate",
    "Notes",
    "Genres",
    "Directors",
    "Studios",
    "Source",
    "SourceID",
    "IMDbID",
    "MyAnimeListID",
    "Added",
]


@login_required
def export_library(request):
    """Everything in your library as one CSV: a backup you keep, and a file the importer reads back
    (it uses the Title and Year columns)."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="curtaincall-backup-{stamp}.csv"'
    response.write("\ufeff")  # so Excel opens accents correctly
    writer = csv.writer(response)
    writer.writerow(BACKUP_COLUMNS)
    entries = Entry.objects.filter(user=request.user).select_related("title").order_by("title__name", "title__year")
    for e in entries:
        t = e.title
        writer.writerow(
            [
                t.name,
                t.year or "",
                t.get_kind_display(),
                "Animated" if t.animated else "Live action",
                e.get_status_display(),
                e.rating if e.rating is not None else "",
                e.watched_on.isoformat() if e.watched_on else "",
                e.notes,
                "; ".join(t.genres),
                "; ".join(t.directors),
                "; ".join(t.studios),
                t.get_source_display(),
                t.source_id,
                t.imdb_id,
                t.mal_id,
                e.created_at.date().isoformat(),
            ]
        )
    return response


@login_required
def import_unmatched(request, pk):
    """The rows still without a match, as a CSV to fix and upload again."""
    batch = get_object_or_404(ImportBatch, pk=pk, user=request.user)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="unmatched-{batch.pk}.csv"'
    response.write("﻿")  # so Excel opens accents correctly
    writer = csv.writer(response)
    writer.writerow(["Title", "Year", "State"])
    for row in batch.rows.filter(state__in=[ImportRow.REVIEW, ImportRow.NOT_FOUND]):
        writer.writerow([row.raw_title, row.raw_year or "", row.get_state_display()])
    return response


@login_required
@require_POST
def import_process(request, pk):
    """Match the next few pending rows. The returned fragment re-triggers itself until done."""
    batch = get_object_or_404(ImportBatch, pk=pk, user=request.user)
    error = ""
    waiting = False  # a rate limit: the page carries on by itself after a short pause
    started = time.monotonic()
    for row in batch.rows.filter(state=ImportRow.PENDING)[:BATCH_SIZE]:
        if time.monotonic() - started > BATCH_SECONDS:
            break
        try:
            matching.match_row(row, batch.hint)
            if row.state == ImportRow.MATCHED:
                # MyAnimeList's cast lookup waits for the refresh, to keep the import moving.
                settle_title(row, request.user, row.matched_title, include_slow=False)
        except sources.RateLimited as exc:
            log.info("import paused: %s", exc)
            waiting = True
            break
        except (sources.SourceError, OSError) as exc:
            log.warning("import row %s failed: %s", row.pk, exc)
            error = str(exc)
            break
    progress = batch_progress(batch)
    context = {"batch": batch, "progress": progress, "error": error, "waiting": waiting}
    response = render(request, "tracker/_import_progress.html", context)
    if not progress["pending"]:
        response["HX-Refresh"] = "true"  # reload the page so the review list appears
    return response


def undo_row(row, user):
    """Put a decided row back up for review, removing only the library entries this row created."""
    Entry.objects.filter(pk__in=row.created_entry_ids, user=user).delete()
    row.state = ImportRow.REVIEW if row.candidates else ImportRow.NOT_FOUND
    row.matched_title = None
    row.picked_title_ids = []
    row.created_entry_ids = []
    row.save()


@login_required
@require_POST
def import_resolve(request, row_pk):
    row = get_object_or_404(ImportRow, pk=row_pk, batch__user=request.user)
    action = request.POST.get("action")
    try:
        if action == "skip":
            row.state = ImportRow.SKIPPED
            row.save()
        elif action == "undo":
            undo_row(row, request.user)
        elif action == "pick":
            indexes = sorted({as_int(x) for x in request.POST.getlist("choice")} - {None})
            if not indexes or not all(0 <= i < len(row.candidates) for i in indexes):
                return HttpResponseBadRequest("pick at least one candidate")
            for i in indexes:
                settle_title(row, request.user, sources.upsert_title(row.candidates[i]))
        elif action == "retry":
            row.raw_title = request.POST.get("title", row.raw_title).strip()[:300] or row.raw_title
            year = request.POST.get("year", "").strip()
            row.raw_year = int(year) if year.isdigit() else None
            kind = request.POST.get("kind")
            hint = matching.HINT_ANIMATED if kind == "mal" else matching.HINT_MOVIE
            matching.match_row(row, hint)
            if row.state == ImportRow.MATCHED:
                settle_title(row, request.user, row.matched_title)
        else:
            return HttpResponseBadRequest("unknown action")
    except (sources.SourceError, OSError, ValueError) as exc:
        log.warning("resolve failed: %s", exc)
        return HttpResponse('<div class="card muted">Could not reach the service, try again.</div>')
    open_rows = (ImportRow.REVIEW, ImportRow.NOT_FOUND)
    compact = request.POST.get("compact") and row.state not in open_rows
    template = "tracker/_settled_row.html" if compact else "tracker/_review_row.html"
    return render(request, template, {"row": row, "kinds": RETRY_KINDS})


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


# ---------------------------------------------------------------- refreshing details and scores

REFRESH_SECONDS = 20  # stay well inside the web server's 60-second request timeout


def run_refresh(omdb_budget):
    """Fill in missing details, then scores, for as many titles as fit in REFRESH_SECONDS."""
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

    return {
        "details_done": details_done,
        "scores_done": scores_done,
        "details_left": needs_details.count(),
        "scores_left": needs_scores.count() if settings.OMDB_API_KEY else 0,
        "stopped": stopped,
    }


@csrf_exempt
@require_POST
def refresh_batch(request):
    """Called repeatedly by GitHub Actions every night. Protected by a shared token.
    Returns how much work is left so the caller knows when to stop."""
    token = request.headers.get("X-Refresh-Token", "")
    if not settings.REFRESH_TOKEN or not hmac.compare_digest(token, settings.REFRESH_TOKEN):
        return JsonResponse({"error": "not allowed"}, status=403)
    try:
        omdb_budget = max(0, int(request.POST.get("omdb_budget", "0")))
    except ValueError:
        omdb_budget = 0
    return JsonResponse(run_refresh(omdb_budget))


@login_required
def refresh_page(request):
    """The owner can run the refresh from the browser instead of waiting for the nightly job."""
    require_owner(request)
    old = Title.objects.filter(source=Title.SOURCE_ANILIST, entries__isnull=False).distinct()
    context = {
        "details_left": Title.objects.filter(details_fetched=False).count(),
        "old_titles": old,
    }
    return render(request, "tracker/refresh.html", context)


@login_required
@require_POST
def refresh_run(request):
    require_owner(request)
    result = run_refresh(omdb_budget=0)  # OMDb's daily allowance is left to the nightly job
    # Every title either updates or is marked as tried, so each step makes progress. A rate limit
    # only pauses the loop: the next step starts after a short wait.
    keep_going = bool(result["details_left"])
    delay = "300ms" if result["stopped"] in ("", "time") else "20s"
    context = {"result": result, "keep_going": keep_going, "delay": delay}
    return render(request, "tracker/_refresh_progress.html", context)
