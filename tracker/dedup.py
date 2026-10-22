"""Spot and merge the same title arriving from different sources (e.g. a Ghibli film in TMDB and AniList).

Two titles count as the same when they share an IMDb or MyAnimeList id, or when their
normalised names match and their years are at most one apart. TMDB and AniList share no
ids with each other, so between those two the name-and-year check does the work.
"""

from collections import defaultdict

from django.db import transaction

from .matching import normalize
from .models import Entry, ImportRow, Title

COPY_IF_EMPTY = [
    "original_name",
    "overview",
    "poster_url",
    "backdrop_url",
    "release_date",
    "runtime_minutes",
    "episodes",
    "trailer_key",
    "box_office_usd",
    "imdb_id",
    "mal_id",
    "imdb_rating",
    "imdb_votes",
    "rt_critics",
]


def names_of(name, original_name="", synonyms=()):
    return {n for n in (normalize(x) for x in [name, original_name, *synonyms]) if n}


def years_close(a, b):
    return a is not None and b is not None and abs(a - b) <= 1


def find_duplicate(source, source_id, name, original_name="", year=None, imdb_id="", mal_id="", within=None):
    """Return an existing Title from another source that is the same work, or None.

    `within` limits the search to a queryset (e.g. titles in one person's library).
    """
    qs = within if within is not None else Title.objects.all()
    qs = qs.exclude(source=source, source_id=source_id)
    if imdb_id:
        hit = qs.filter(imdb_id=imdb_id).first()
        if hit:
            return hit
    if mal_id:
        hit = qs.filter(mal_id=mal_id).first()
        if hit:
            return hit
    if year is None:
        return None
    wanted = names_of(name, original_name)
    for other in qs.exclude(source=source).filter(year__gte=year - 1, year__lte=year + 1):
        if wanted & names_of(other.name, other.original_name):
            return other
    return None


def duplicate_in_library(user, candidate):
    """For the add flow: the same work already in this person's library, if any."""
    extra = candidate.get("extra") or {}
    library = Title.objects.filter(entries__user=user)
    return find_duplicate(
        candidate["source"],
        candidate["source_id"],
        candidate["name"],
        candidate.get("original_name", ""),
        candidate.get("year"),
        mal_id=extra.get("mal_id", ""),
        within=library,
    )


def likely_pairs(user):
    """Pairs of titles in one library that look like the same work. Each pair is (a, b)."""
    titles = list(Title.objects.filter(entries__user=user).distinct())
    by_name = defaultdict(list)
    for t in titles:
        for n in names_of(t.name, t.original_name):
            by_name[n].append(t)
    by_id = defaultdict(list)
    for t in titles:
        if t.imdb_id:
            by_id["imdb:" + t.imdb_id].append(t)
        if t.mal_id:
            by_id["mal:" + t.mal_id].append(t)

    seen, pairs = set(), []
    for group in list(by_id.values()) + list(by_name.values()):
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                key = tuple(sorted((a.pk, b.pk)))
                if key in seen or a.source == b.source and a.source_id == b.source_id:
                    continue
                same_id = (a.imdb_id and a.imdb_id == b.imdb_id) or (a.mal_id and a.mal_id == b.mal_id)
                if same_id or (a.source != b.source and years_close(a.year, b.year)):
                    seen.add(key)
                    pairs.append((a, b))
    return pairs


@transaction.atomic
def merge_titles(keep, drop):
    """Move everything from `drop` onto `keep`, then delete `drop`."""
    if keep.pk == drop.pk:
        return keep
    for field in COPY_IF_EMPTY:
        if not getattr(keep, field) and getattr(drop, field):
            setattr(keep, field, getattr(drop, field))
    if not keep.cast and drop.cast:
        keep.cast = drop.cast
    keep.save()

    for entry in Entry.objects.filter(title=drop):
        existing = Entry.objects.filter(user=entry.user, title=keep).first()
        if existing is None:
            entry.title = keep
            entry.save()
            continue
        # Both titles were in the library: keep whatever information either entry had.
        existing.rating = existing.rating or entry.rating
        existing.watched_on = existing.watched_on or entry.watched_on
        if entry.notes and entry.notes not in existing.notes:
            existing.notes = (existing.notes + "\n\n" + entry.notes).strip()
        if existing.status != Entry.STATUS_WATCHED and entry.status == Entry.STATUS_WATCHED:
            existing.status = Entry.STATUS_WATCHED
        existing.save()
        entry.delete()

    ImportRow.objects.filter(matched_title=drop).update(matched_title=keep)
    drop.delete()
    return keep


def fold_import_duplicate(title, user):
    """After an import match: if the library already holds the same work from another source,
    use that title instead and drop the fresh copy when nothing else uses it."""
    library = Title.objects.filter(entries__user=user)
    existing = find_duplicate(
        title.source, title.source_id, title.name, title.original_name, title.year, title.imdb_id, title.mal_id, library
    )
    if existing is None:
        return title
    if not title.entries.exists():
        ImportRow.objects.filter(matched_title=title).update(matched_title=existing)
        title.delete()
    return existing
