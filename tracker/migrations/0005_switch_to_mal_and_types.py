"""Move to Film/Series + Animated/Live action and from AniList to MyAnimeList.

- TMDB titles remember their TMDB type (movie/tv) separately from the Film/Series shown on the site,
  and are queued for a refresh so their lead studios get filled in.
- AniList titles become MyAnimeList titles through the MyAnimeList id AniList gave us, keeping every
  rating, date and note. The few without that id stay as they are and can be re-matched by hand.
- Studios move out of the directors field, genres get one shared spelling, and anything tagged
  Animation is marked animated.
"""

from django.db import migrations

GENRE_ALIASES = {
    "Sci-Fi": ["Science Fiction"],
    "Sci-Fi & Fantasy": ["Science Fiction", "Fantasy"],
    "Action & Adventure": ["Action", "Adventure"],
    "War & Politics": ["War", "Politics"],
    "Suspense": ["Thriller"],
    "Kids": ["Family"],
    "Award Winning": [],
}
MAL_FORMATS = {"MOVIE": "movie", "TV": "tv", "TV_SHORT": "tv"}


def clean_genres(names):
    out = []
    for name in names or []:
        for g in GENRE_ALIASES.get(name, [name]):
            if g not in out:
                out.append(g)
    return out


def kind_from(fmt, episodes):
    if fmt in MAL_FORMATS:
        return MAL_FORMATS[fmt]
    return "movie" if episodes == 1 else "tv"


def forwards(apps, schema_editor):
    Title = apps.get_model("tracker", "Title")
    Entry = apps.get_model("tracker", "Entry")
    ImportRow = apps.get_model("tracker", "ImportRow")

    for t in Title.objects.filter(source="tmdb"):
        t.tmdb_type = t.kind if t.kind in ("movie", "tv") else "movie"
        t.kind = t.tmdb_type
        t.genres = clean_genres(t.genres)
        t.animated = t.animated or "Animation" in t.genres
        t.details_fetched = False  # fills in lead studios
        t.save()

    converted = {}  # MyAnimeList id -> title
    for t in Title.objects.filter(source="anilist").order_by("id"):
        t.kind = kind_from(None, t.episodes)
        t.studios = t.directors
        t.directors = []
        t.genres = clean_genres(t.genres)
        t.animated = True
        if not t.mal_id:
            t.details_fetched = True  # nothing to refresh from any more
            t.save()
            continue
        twin = converted.get(t.mal_id) or Title.objects.filter(source="mal", source_id=t.mal_id).first()
        if twin is not None:
            # Two AniList entries pointed at one MyAnimeList title: keep one, combine the library entries.
            for entry in Entry.objects.filter(title=t):
                existing = Entry.objects.filter(user=entry.user, title=twin).first()
                if existing is None:
                    entry.title = twin
                    entry.save()
                else:
                    existing.rating = existing.rating or entry.rating
                    existing.watched_on = existing.watched_on or entry.watched_on
                    if entry.notes and entry.notes not in existing.notes:
                        existing.notes = (existing.notes + "\n\n" + entry.notes).strip()
                    existing.save()
                    entry.delete()
            ImportRow.objects.filter(matched_title=t).update(matched_title=twin)
            t.delete()
            continue
        t.source, t.source_id = "mal", t.mal_id
        t.details_fetched = False  # cast and studios come from MyAnimeList on the next refresh
        t.save()
        converted[t.mal_id] = t

    for t in Title.objects.filter(kind="anime"):  # safety net
        t.kind = kind_from(None, t.episodes)
        t.animated = True
        t.save()

    # Import rows: remember what each match added, so old matches can be undone too, and turn
    # AniList candidates into MyAnimeList ones.
    for row in ImportRow.objects.select_related("batch"):
        changed = False
        if row.matched_title_id and not row.picked_title_ids:
            row.picked_title_ids = [row.matched_title_id]
            entry = Entry.objects.filter(user_id=row.batch.user_id, title_id=row.matched_title_id).first()
            if entry is not None and entry.created_at >= row.batch.created_at:
                row.created_entry_ids = [entry.pk]
            changed = True
        if any(c.get("source") == "anilist" for c in row.candidates or []):
            fixed = []
            for c in row.candidates:
                if c.get("source") != "anilist":
                    fixed.append(c)
                    continue
                extra = c.get("extra") or {}
                if not extra.get("mal_id"):
                    continue
                c = dict(c, source="mal", source_id=extra["mal_id"], kind=kind_from(extra.get("format"), extra.get("episodes")))
                c["extra"] = dict(extra, studios=extra.get("studios", []), genres=clean_genres(extra.get("genres")))
                fixed.append(c)
            row.candidates = fixed
            changed = True
        if changed:
            row.save()


class Migration(migrations.Migration):
    dependencies = [("tracker", "0004_mal_types_studios_locks")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
