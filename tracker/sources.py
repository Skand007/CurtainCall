"""Clients for TMDB (movies, series) and AniList (anime).

Every search returns a list of "candidate" dicts with the same keys, so the
matching code does not care where a result came from:

    source, source_id, kind, name, original_name, year, poster_url,
    overview, animated, score, extra
"""

import requests
from django.conf import settings
from django.db import transaction

from .models import Title

TMDB_BASE = "https://api.themoviedb.org/3"
ANILIST_URL = "https://graphql.anilist.co"
TMDB_ANIMATION_GENRE = 16
TIMEOUT = 10

session = requests.Session()


class SourceError(Exception):
    pass


def tmdb_get(path, params=None):
    if not settings.TMDB_TOKEN:
        raise SourceError("TMDB_TOKEN is not set")
    headers = {"Authorization": "Bearer " + settings.TMDB_TOKEN, "accept": "application/json"}
    resp = session.get(TMDB_BASE + path, params=params or {}, headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SourceError(f"TMDB {path} returned {resp.status_code}")
    return resp.json()


def year_from_date(value):
    if value and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def tmdb_candidate(item, kind):
    if kind == Title.KIND_MOVIE:
        name, original, date = item.get("title", ""), item.get("original_title", ""), item.get("release_date")
    else:
        name, original, date = item.get("name", ""), item.get("original_name", ""), item.get("first_air_date")
    poster = item.get("poster_path")
    return {
        "source": Title.SOURCE_TMDB,
        "source_id": str(item["id"]),
        "kind": kind,
        "name": name,
        "original_name": original,
        "year": year_from_date(date),
        "poster_url": settings.TMDB_IMAGE_BASE + poster if poster else "",
        "overview": item.get("overview", "") or "",
        "animated": TMDB_ANIMATION_GENRE in (item.get("genre_ids") or []),
        "score": item.get("vote_average"),
        "popularity": item.get("popularity") or 0,
        "extra": {},
    }


def search_tmdb(query, kind=Title.KIND_MOVIE, year=None, limit=8):
    path = "/search/movie" if kind == Title.KIND_MOVIE else "/search/tv"
    params = {"query": query, "include_adult": "false"}
    data = tmdb_get(path, params)
    results = [tmdb_candidate(item, kind) for item in data.get("results", [])[:limit]]
    # Year is used for ranking only; filtering by it in the API drops off-by-one release years.
    if year:
        results.sort(key=lambda c: (c["year"] is None or abs(c["year"] - year) > 1, -c["popularity"]))
    return results


ANILIST_MEDIA_FIELDS = """
      id
      title { romaji english native }
      synonyms
      format
      episodes
      duration
      seasonYear
      startDate { year }
      genres
      averageScore
      description(asHtml: false)
      coverImage { large }
      studios(isMain: true) { nodes { name } }
"""

ANILIST_SEARCH_QUERY = (
    "query ($search: String, $perPage: Int) { Page(perPage: $perPage) {"
    " media(search: $search, type: ANIME, sort: SEARCH_MATCH) {" + ANILIST_MEDIA_FIELDS + "} } }"
)
ANILIST_ID_QUERY = (
    "query ($id: Int) { Page(perPage: 1) { media(id: $id, type: ANIME) {" + ANILIST_MEDIA_FIELDS + "} } }"
)


def anilist_candidate(media):
    titles = media.get("title") or {}
    english = titles.get("english") or ""
    romaji = titles.get("romaji") or ""
    year = media.get("seasonYear") or (media.get("startDate") or {}).get("year")
    episodes = media.get("episodes")
    duration = media.get("duration")
    runtime = episodes * duration if episodes and duration else None
    score = media.get("averageScore")
    return {
        "source": Title.SOURCE_ANILIST,
        "source_id": str(media["id"]),
        "kind": Title.KIND_ANIME,
        "name": english or romaji,
        "original_name": romaji if english else "",
        "year": year,
        "poster_url": (media.get("coverImage") or {}).get("large") or "",
        "overview": (media.get("description") or "").replace("<br>", "\n"),
        "animated": True,
        "score": score / 10 if score else None,
        "popularity": 0,
        "extra": {
            "synonyms": media.get("synonyms") or [],
            "format": media.get("format"),
            "episodes": episodes,
            "runtime": runtime,
            "genres": media.get("genres") or [],
            "studios": [n["name"] for n in ((media.get("studios") or {}).get("nodes") or [])],
        },
    }


def search_anilist(query, limit=8):
    body = {"query": ANILIST_SEARCH_QUERY, "variables": {"search": query, "perPage": limit}}
    resp = session.post(ANILIST_URL, json=body, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SourceError(f"AniList returned {resp.status_code}")
    media = ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []
    return [anilist_candidate(m) for m in media]


def get_tmdb(kind, source_id):
    """Look up one TMDB title by id and return it as a candidate."""
    data = tmdb_get(f"/{kind}/{source_id}")
    data["genre_ids"] = [g["id"] for g in data.get("genres", [])]
    return tmdb_candidate(data, kind)


def get_anilist(source_id):
    body = {"query": ANILIST_ID_QUERY, "variables": {"id": int(source_id)}}
    resp = session.post(ANILIST_URL, json=body, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SourceError(f"AniList returned {resp.status_code}")
    media = ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []
    if not media:
        raise SourceError(f"AniList id {source_id} not found")
    return anilist_candidate(media[0])


def get_candidate(source, kind, source_id):
    if source == Title.SOURCE_ANILIST:
        return get_anilist(source_id)
    if kind not in (Title.KIND_MOVIE, Title.KIND_TV):
        raise SourceError(f"Unknown TMDB kind {kind}")
    return get_tmdb(kind, source_id)


def upsert_title(candidate):
    """Create or refresh a Title from a candidate dict."""
    extra = candidate.get("extra") or {}
    defaults = {
        "kind": candidate["kind"],
        "name": candidate["name"][:300],
        "original_name": (candidate.get("original_name") or "")[:300],
        "year": candidate.get("year"),
        "overview": candidate.get("overview") or "",
        "poster_url": candidate.get("poster_url") or "",
        "animated": bool(candidate.get("animated")),
        "external_score": candidate.get("score"),
    }
    if candidate["source"] == Title.SOURCE_ANILIST:
        # AniList search already carries the full details.
        defaults.update(
            genres=extra.get("genres", []),
            directors=extra.get("studios", []),
            episodes=extra.get("episodes"),
            runtime_minutes=extra.get("runtime"),
            details_fetched=True,
        )
    with transaction.atomic():
        title, _ = Title.objects.update_or_create(
            source=candidate["source"], source_id=candidate["source_id"], defaults=defaults
        )
    return title


def fetch_tmdb_details(title):
    """Fill genres, directors/creators and runtime for a TMDB title."""
    data = tmdb_get(f"/{title.kind}/{title.source_id}", {"append_to_response": "credits"})
    title.genres = [g["name"] for g in data.get("genres", [])]
    title.animated = any(g["id"] == TMDB_ANIMATION_GENRE for g in data.get("genres", []))
    if title.kind == Title.KIND_MOVIE:
        crew = (data.get("credits") or {}).get("crew", [])
        title.directors = [c["name"] for c in crew if c.get("job") == "Director"]
        title.runtime_minutes = data.get("runtime") or None
    else:
        title.directors = [c["name"] for c in data.get("created_by", [])]
        title.episodes = data.get("number_of_episodes")
        run_times = data.get("episode_run_time") or []
        if run_times and title.episodes:
            title.runtime_minutes = run_times[0] * title.episodes
    title.details_fetched = True
    title.save()
    return title


def ensure_details(title):
    if not title.details_fetched and title.source == Title.SOURCE_TMDB:
        fetch_tmdb_details(title)
    return title
