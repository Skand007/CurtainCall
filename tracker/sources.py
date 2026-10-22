"""Clients for TMDB (films, series), AniList (anime) and OMDb (IMDb and Rotten Tomatoes scores).

Every search returns a list of "candidate" dicts with the same keys, so the
matching code does not care where a result came from:

    source, source_id, kind, name, original_name, year, poster_url,
    overview, animated, score, popularity, extra
"""

import datetime
import time

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Title

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGES = "https://image.tmdb.org/t/p/"
ANILIST_URL = "https://graphql.anilist.co"
OMDB_URL = "https://www.omdbapi.com/"
TMDB_ANIMATION_GENRE = 16
TIMEOUT = 10
CAST_SIZE = 12

session = requests.Session()


class SourceError(Exception):
    pass


class RateLimited(SourceError):
    """The service said 'come back later'. Callers should stop for now, not retry."""


def parse_date(value):
    """'1995-12-15' -> date. Anything incomplete -> None."""
    try:
        return datetime.date.fromisoformat(value) if value and len(value) == 10 else None
    except ValueError:
        return None


def year_from_date(value):
    if value and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def tmdb_image(path, size):
    return TMDB_IMAGES + size + path if path else ""


# ---------------------------------------------------------------- TMDB


def tmdb_get(path, params=None):
    if not settings.TMDB_TOKEN:
        raise SourceError("TMDB_TOKEN is not set")
    headers = {"Authorization": "Bearer " + settings.TMDB_TOKEN, "accept": "application/json"}
    resp = session.get(TMDB_BASE + path, params=params or {}, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 429:
        raise RateLimited("TMDB rate limit reached")
    if resp.status_code != 200:
        raise SourceError(f"TMDB {path} returned {resp.status_code}")
    return resp.json()


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
        "extra": {
            "release_date": date or "",
            "backdrop_url": tmdb_image(item.get("backdrop_path"), "w1280"),
        },
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


def pick_trailer(videos):
    """Best YouTube video: official trailer, then any trailer, then a teaser."""
    youtube = [v for v in videos if v.get("site") == "YouTube" and v.get("key")]
    for wanted in (
        lambda v: v.get("type") == "Trailer" and v.get("official"),
        lambda v: v.get("type") == "Trailer",
        lambda v: v.get("type") == "Teaser",
    ):
        for v in youtube:
            if wanted(v):
                return v["key"]
    return ""


def fetch_tmdb_details(title):
    """Fill genres, people, runtime, release date, cast, trailer, box office and IMDb id."""
    data = tmdb_get(
        f"/{title.kind}/{title.source_id}",
        {"append_to_response": "credits,videos,external_ids", "include_video_language": "en,null"},
    )
    genres = data.get("genres", [])
    title.genres = [g["name"] for g in genres]
    title.animated = any(g["id"] == TMDB_ANIMATION_GENRE for g in genres)
    credits = data.get("credits") or {}
    if title.kind == Title.KIND_MOVIE:
        title.directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
        title.runtime_minutes = data.get("runtime") or None
        title.release_date = parse_date(data.get("release_date"))
        title.box_office_usd = data.get("revenue") or None  # TMDB uses 0 for "unknown"
        title.imdb_id = data.get("imdb_id") or ""
    else:
        title.directors = [c["name"] for c in data.get("created_by", [])]
        title.episodes = data.get("number_of_episodes")
        run_times = data.get("episode_run_time") or []
        if run_times and title.episodes:
            title.runtime_minutes = run_times[0] * title.episodes
        title.release_date = parse_date(data.get("first_air_date"))
        title.imdb_id = (data.get("external_ids") or {}).get("imdb_id") or ""
    title.cast = [
        {
            "name": c.get("name", ""),
            "character": c.get("character", "") or "",
            "photo": tmdb_image(c.get("profile_path"), "w185"),
        }
        for c in sorted(credits.get("cast", []), key=lambda c: c.get("order", 999))[:CAST_SIZE]
    ]
    title.trailer_key = pick_trailer((data.get("videos") or {}).get("results", []))
    title.backdrop_url = tmdb_image(data.get("backdrop_path"), "w1280") or title.backdrop_url
    if not title.poster_url and data.get("poster_path"):
        title.poster_url = settings.TMDB_IMAGE_BASE + data["poster_path"]
    title.details_fetched = True
    title.save()
    return title


def get_tmdb(kind, source_id):
    """Look up one TMDB title by id and return it as a candidate."""
    data = tmdb_get(f"/{kind}/{source_id}")
    data["genre_ids"] = [g["id"] for g in data.get("genres", [])]
    return tmdb_candidate(data, kind)


# ---------------------------------------------------------------- AniList

ANILIST_MEDIA_FIELDS = """
      id
      idMal
      title { romaji english native }
      synonyms
      format
      episodes
      duration
      seasonYear
      startDate { year month day }
      genres
      averageScore
      description(asHtml: false)
      coverImage { large }
      bannerImage
      trailer { id site }
      studios(isMain: true) { nodes { name } }
"""
# The cast is only needed on the title page, so only the by-id lookup asks for it.
ANILIST_CAST_FIELDS = """
      characters(role: MAIN, perPage: 12, sort: [ROLE, RELEVANCE]) {
        edges { node { name { full } image { medium } } voiceActors(language: JAPANESE) { name { full } } }
      }
"""

ANILIST_SEARCH_QUERY = (
    "query ($search: String, $perPage: Int) { Page(perPage: $perPage) {"
    " media(search: $search, type: ANIME, sort: SEARCH_MATCH) {" + ANILIST_MEDIA_FIELDS + "} } }"
)
ANILIST_ID_QUERY = (
    "query ($id: Int) { Page(perPage: 1) { media(id: $id, type: ANIME) {"
    + ANILIST_MEDIA_FIELDS
    + ANILIST_CAST_FIELDS
    + "} } }"
)


def anilist_post(body):
    resp = session.post(ANILIST_URL, json=body, timeout=TIMEOUT)
    if resp.status_code == 429:
        raise RateLimited("AniList rate limit reached")
    if resp.status_code != 200:
        raise SourceError(f"AniList returned {resp.status_code}")
    return ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []


def anilist_candidate(media):
    titles = media.get("title") or {}
    english = titles.get("english") or ""
    romaji = titles.get("romaji") or ""
    start = media.get("startDate") or {}
    year = media.get("seasonYear") or start.get("year")
    episodes = media.get("episodes")
    duration = media.get("duration")
    runtime = episodes * duration if episodes and duration else None
    score = media.get("averageScore")
    trailer = media.get("trailer") or {}
    release = ""
    if start.get("year") and start.get("month") and start.get("day"):
        release = f"{start['year']:04d}-{start['month']:02d}-{start['day']:02d}"
    cast = None
    if "characters" in media:
        cast = []
        for edge in ((media.get("characters") or {}).get("edges") or [])[:CAST_SIZE]:
            node = edge.get("node") or {}
            actors = edge.get("voiceActors") or []
            character = (node.get("name") or {}).get("full") or ""
            cast.append(
                {
                    "name": ((actors[0].get("name") or {}).get("full") if actors else "") or character,
                    "character": character,
                    "photo": (node.get("image") or {}).get("medium") or "",
                }
            )
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
            "mal_id": str(media["idMal"]) if media.get("idMal") else "",
            "release_date": release,
            "backdrop_url": media.get("bannerImage") or "",
            "trailer_key": (trailer.get("id") or "") if trailer.get("site") == "youtube" else "",
            "cast": cast,  # None means "not asked for", so an update keeps the stored cast
        },
    }


def search_anilist(query, limit=8):
    body = {"query": ANILIST_SEARCH_QUERY, "variables": {"search": query, "perPage": limit}}
    return [anilist_candidate(m) for m in anilist_post(body)]


def get_anilist(source_id):
    media = anilist_post({"query": ANILIST_ID_QUERY, "variables": {"id": int(source_id)}})
    if not media:
        raise SourceError(f"AniList id {source_id} not found")
    return anilist_candidate(media[0])


def get_candidate(source, kind, source_id):
    if source == Title.SOURCE_ANILIST:
        return get_anilist(source_id)
    if kind not in (Title.KIND_MOVIE, Title.KIND_TV):
        raise SourceError(f"Unknown TMDB kind {kind}")
    return get_tmdb(kind, source_id)


# ---------------------------------------------------------------- OMDb (IMDb and Rotten Tomatoes)


def parse_omdb(data):
    """OMDb response -> (imdb_rating, imdb_votes, rt_critics). 'N/A' becomes None."""
    rating = data.get("imdbRating")
    imdb_rating = float(rating) if rating and rating != "N/A" else None
    votes = (data.get("imdbVotes") or "").replace(",", "")
    imdb_votes = int(votes) if votes.isdigit() else None
    rt_critics = None
    for r in data.get("Ratings") or []:
        value = (r.get("Value") or "").rstrip("%")
        if r.get("Source") == "Rotten Tomatoes" and value.isdigit():
            rt_critics = int(value)
    return imdb_rating, imdb_votes, rt_critics


def fetch_omdb_scores(title):
    """Store IMDb rating and Rotten Tomatoes critics score. Needs an IMDb id and OMDB_API_KEY."""
    if not settings.OMDB_API_KEY:
        raise SourceError("OMDB_API_KEY is not set")
    if not title.imdb_id:
        return title
    resp = session.get(OMDB_URL, params={"i": title.imdb_id, "apikey": settings.OMDB_API_KEY}, timeout=TIMEOUT)
    if resp.status_code == 401 and "limit" in resp.text.lower():
        raise RateLimited("OMDb daily limit reached")
    if resp.status_code != 200:
        raise SourceError(f"OMDb returned {resp.status_code}")
    data = resp.json()
    if data.get("Response") == "False":
        if "limit" in (data.get("Error") or "").lower():
            raise RateLimited("OMDb daily limit reached")
    else:
        title.imdb_rating, title.imdb_votes, title.rt_critics = parse_omdb(data)
    title.scores_checked_at = timezone.now()
    title.save()
    return title


# ---------------------------------------------------------------- storing titles


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
    if extra.get("release_date"):
        defaults["release_date"] = parse_date(extra["release_date"])
    if extra.get("backdrop_url"):
        defaults["backdrop_url"] = extra["backdrop_url"]
    if candidate["source"] == Title.SOURCE_ANILIST:
        defaults.update(
            genres=extra.get("genres", []),
            directors=extra.get("studios", []),
            episodes=extra.get("episodes"),
            runtime_minutes=extra.get("runtime"),
            mal_id=extra.get("mal_id", ""),
            trailer_key=extra.get("trailer_key", ""),
        )
        if extra.get("cast") is not None:
            # Only the by-id lookup includes the cast, so only it counts as "details fetched".
            defaults.update(cast=extra["cast"], details_fetched=True)
    with transaction.atomic():
        title, _ = Title.objects.update_or_create(
            source=candidate["source"], source_id=candidate["source_id"], defaults=defaults
        )
    return title


def ensure_details(title, include_anilist=True):
    """Fetch full details if they are missing. Imports skip AniList to stay under its rate limit."""
    if title.details_fetched:
        return title
    if title.source == Title.SOURCE_TMDB:
        return fetch_tmdb_details(title)
    if include_anilist:
        return upsert_title(get_anilist(title.source_id))
    return title


def refresh_title(title):
    """Full refresh used by the nightly job: details first, then scores if possible."""
    title = ensure_details(title)
    if settings.OMDB_API_KEY and title.imdb_id and title.scores_checked_at is None:
        title = fetch_omdb_scores(title)
    return title


def pause_for(title):
    """Polite gap between calls in long batches (AniList allows roughly 90 requests a minute)."""
    time.sleep(0.75 if title.source == Title.SOURCE_ANILIST else 0.05)
