"""Clients for TMDB (films, series), MyAnimeList via Jikan (Japanese animation) and OMDb (IMDb and
Rotten Tomatoes scores).

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


# TMDB and MyAnimeList name some genres differently; one list for the whole site.
GENRE_ALIASES = {
    "Sci-Fi": ["Science Fiction"],
    "Sci-Fi & Fantasy": ["Science Fiction", "Fantasy"],
    "Action & Adventure": ["Action", "Adventure"],
    "War & Politics": ["War", "Politics"],
    "Suspense": ["Thriller"],
    "Kids": ["Family"],
    "Award Winning": [],  # a MyAnimeList label, not a genre
}


def clean_genres(names):
    out = []
    for name in names:
        for g in GENRE_ALIASES.get(name, [name]):
            if g not in out:
                out.append(g)
    return out


# TMDB lists every production company, financiers included, and marks none as the lead.
# The first one listed is the lead; later ones count only when they are a well-known studio.
MAJOR_STUDIOS = {
    "20th Century Fox",
    "20th Century Studios",
    "20th Century Fox Animation",
    "A24",
    "Aardman",
    "Amazon MGM Studios",
    "Amblin Entertainment",
    "Apple Studios",
    "Blue Sky Studios",
    "Blumhouse Productions",
    "Cartoon Network Studios",
    "Columbia Pictures",
    "DC Studios",
    "DreamWorks Animation",
    "DreamWorks Pictures",
    "Focus Features",
    "Fox Searchlight Pictures",
    "HBO",
    "Illumination",
    "Laika",
    "Legendary Pictures",
    "Lionsgate",
    "Lucasfilm Ltd.",
    "Marvel Studios",
    "Metro-Goldwyn-Mayer",
    "Miramax",
    "Neon",
    "Netflix",
    "New Line Cinema",
    "Nickelodeon Animation Studio",
    "Orion Pictures",
    "Paramount Animation",
    "Paramount Pictures",
    "Pixar",
    "Searchlight Pictures",
    "Sony Pictures Animation",
    "Studio Ghibli",
    "Summit Entertainment",
    "Toho",
    "TriStar Pictures",
    "Universal Pictures",
    "Walt Disney Animation Studios",
    "Walt Disney Pictures",
    "Warner Bros. Animation",
    "Warner Bros. Pictures",
}


def lead_studios(companies):
    names = [c.get("name", "") for c in companies or [] if c.get("name")]
    if not names:
        return []
    return [names[0]] + [n for n in names[1:] if n in MAJOR_STUDIOS]


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
    tmdb_type = title.tmdb_type or title.kind
    data = tmdb_get(
        f"/{tmdb_type}/{title.source_id}",
        {"append_to_response": "credits,videos,external_ids", "include_video_language": "en,null"},
    )
    genres = data.get("genres", [])
    title.genres = clean_genres(g["name"] for g in genres)
    if not title.is_locked("animated"):
        title.animated = any(g["id"] == TMDB_ANIMATION_GENRE for g in genres)
    if not title.is_locked("studios"):
        title.studios = lead_studios(data.get("production_companies"))
    credits = data.get("credits") or {}
    if tmdb_type == Title.KIND_MOVIE:
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


# ---------------------------------------------------------------- MyAnimeList (through Jikan)

JIKAN_BASE = "https://api.jikan.moe/v4"
JIKAN_GAP = 0.4  # Jikan allows 3 requests a second...
JIKAN_PER_MINUTE = 55  # ...and 60 a minute; stay a little under both
JIKAN_MAX_WAIT = 8  # longest pause inside one request
_jikan_calls = []  # monotonic times of recent calls


def jikan_wait():
    now = time.monotonic()
    while _jikan_calls and now - _jikan_calls[0] > 60:
        _jikan_calls.pop(0)
    if len(_jikan_calls) >= JIKAN_PER_MINUTE:
        wait = 60 - (now - _jikan_calls[0]) + 0.1
        if wait > JIKAN_MAX_WAIT:
            # Never sleep long inside a web request; the caller pauses and comes back.
            raise RateLimited("MyAnimeList allows 60 searches a minute; continuing shortly")
        time.sleep(wait)
    elif _jikan_calls and now - _jikan_calls[-1] < JIKAN_GAP:
        time.sleep(JIKAN_GAP - (now - _jikan_calls[-1]))
    _jikan_calls.append(time.monotonic())


def jikan_get(path, params=None):
    for attempt in range(2):
        jikan_wait()
        resp = session.get(JIKAN_BASE + path, params=params or {}, timeout=TIMEOUT)
        if resp.status_code == 429 and attempt == 0:
            time.sleep(2)  # a short burst limit; one patient retry
            continue
        if resp.status_code == 429:
            raise RateLimited("MyAnimeList (Jikan) rate limit reached")
        if resp.status_code != 200:
            raise SourceError(f"MyAnimeList (Jikan) {path} returned {resp.status_code}")
        return resp.json()
    raise RateLimited("MyAnimeList (Jikan) rate limit reached")


def parse_duration(text):
    """'24 min per ep' -> 24, '1 hr 55 min' -> 115, '30 sec per ep' -> 1, 'Unknown' -> None."""
    if not text:
        return None
    words = text.replace(".", "").split()
    minutes = 0.0
    for i, w in enumerate(words[1:], start=1):
        if words[i - 1].isdigit():
            n = int(words[i - 1])
            if w.startswith("hr"):
                minutes += n * 60
            elif w.startswith("min"):
                minutes += n
            elif w.startswith("sec"):
                minutes += n / 60
    return max(1, round(minutes)) if minutes else None


def mal_kind(media_type, episodes):
    """MyAnimeList's type -> Film or Series. One-episode specials, OVAs and shorts count as films."""
    if media_type == "Movie":
        return Title.KIND_MOVIE
    if media_type == "TV":
        return Title.KIND_TV
    return Title.KIND_MOVIE if episodes == 1 else Title.KIND_TV


def mal_candidate(item, characters=None):
    english = item.get("title_english") or ""
    default = item.get("title") or ""
    names = [t.get("title") for t in item.get("titles") or [] if t.get("title")]
    synonyms = [n for n in names + (item.get("title_synonyms") or []) if n not in (english, default)]
    aired = (item.get("aired") or {}).get("from") or ""
    year = item.get("year") or year_from_date(aired)
    episodes = item.get("episodes")
    per_episode = parse_duration(item.get("duration"))
    runtime = per_episode * episodes if per_episode and episodes else None
    cast = None
    if characters is not None:
        cast = []
        main = [c for c in characters if c.get("role") == "Main"] or characters
        for c in main[:CAST_SIZE]:
            person = c.get("character") or {}
            actors = (
                [v for v in c.get("voice_actors") or [] if v.get("language") == "Japanese"]
                or c.get("voice_actors")
                or []
            )
            character = person_name(person.get("name"))
            cast.append(
                {
                    "name": person_name((actors[0].get("person") or {}).get("name")) if actors else character,
                    "character": character,
                    "photo": ((person.get("images") or {}).get("jpg") or {}).get("image_url") or "",
                }
            )
    images = (item.get("images") or {}).get("jpg") or {}
    return {
        "source": Title.SOURCE_MAL,
        "source_id": str(item["mal_id"]),
        "kind": mal_kind(item.get("type"), episodes),
        "name": english or default,
        "original_name": default if english else "",
        "year": year,
        "poster_url": images.get("large_image_url") or images.get("image_url") or "",
        "overview": (item.get("synopsis") or "").replace("[Written by MAL Rewrite]", "").strip(),
        "animated": True,
        "score": item.get("score"),
        "popularity": item.get("members") or 0,
        "extra": {
            "synonyms": synonyms,
            "format": item.get("type") or "",
            "episodes": episodes,
            "runtime": runtime,
            "genres": clean_genres(g["name"] for g in item.get("genres") or []),
            "studios": [s["name"] for s in item.get("studios") or []],
            "mal_id": str(item["mal_id"]),
            "release_date": aired[:10],
            "trailer_key": youtube_id(item.get("trailer")),
            "cast": cast,  # None means "not asked for", so an update keeps the stored cast
        },
    }


def youtube_id(trailer):
    """Jikan's trailer: the id directly, or taken from the embed link when only that is filled."""
    trailer = trailer or {}
    if trailer.get("youtube_id"):
        return trailer["youtube_id"]
    embed = trailer.get("embed_url") or ""
    if "/embed/" in embed:
        return embed.split("/embed/", 1)[1].split("?", 1)[0][:40]
    return ""


def person_name(name):
    """MyAnimeList writes 'Ogino, Chihiro'; show 'Chihiro Ogino'."""
    if not name:
        return ""
    if ", " in name:
        last, first = name.split(", ", 1)
        return f"{first} {last}"
    return name


def search_mal(query, limit=8):
    data = jikan_get("/anime", {"q": query, "limit": limit, "sfw": "true"})
    return [mal_candidate(item) for item in data.get("data") or []]


def get_mal(source_id):
    """One MyAnimeList title with its main characters and Japanese voice actors."""
    item = jikan_get(f"/anime/{int(source_id)}/full").get("data")
    if not item:
        raise SourceError(f"MyAnimeList id {source_id} not found")
    characters = jikan_get(f"/anime/{int(source_id)}/characters").get("data") or []
    return mal_candidate(item, characters)


def get_candidate(source, kind, source_id):
    if source == Title.SOURCE_MAL:
        return get_mal(source_id)
    if source != Title.SOURCE_TMDB or kind not in (Title.KIND_MOVIE, Title.KIND_TV):
        raise SourceError(f"Unknown title reference {source}/{kind}")
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
    """Create or refresh a Title from a candidate dict. Fields the owner set by hand are kept."""
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
    if candidate["source"] == Title.SOURCE_TMDB:
        defaults["tmdb_type"] = candidate["kind"]
    if extra.get("release_date"):
        defaults["release_date"] = parse_date(extra["release_date"])
    if extra.get("backdrop_url"):
        defaults["backdrop_url"] = extra["backdrop_url"]
    if candidate["source"] == Title.SOURCE_MAL:
        defaults.update(
            genres=extra.get("genres", []),
            studios=extra.get("studios", []),
            episodes=extra.get("episodes"),
            runtime_minutes=extra.get("runtime"),
            mal_id=extra.get("mal_id", ""),
            trailer_key=extra.get("trailer_key", ""),
        )
        if extra.get("cast") is not None:
            # Only the by-id lookup includes the cast, so only it counts as "details fetched".
            defaults.update(cast=extra["cast"], details_fetched=True)
    with transaction.atomic():
        existing = Title.objects.filter(source=candidate["source"], source_id=candidate["source_id"]).first()
        for field in existing.locked if existing else []:
            defaults.pop(field, None)
        title, _ = Title.objects.update_or_create(
            source=candidate["source"], source_id=candidate["source_id"], defaults=defaults
        )
    return title


def ensure_details(title, include_slow=True):
    """Fetch full details if they are missing. Imports skip MyAnimeList's extra calls to keep moving."""
    if title.details_fetched:
        return title
    if title.source == Title.SOURCE_TMDB:
        return fetch_tmdb_details(title)
    if title.source == Title.SOURCE_MAL:
        return upsert_title(get_mal(title.source_id)) if include_slow else title
    # Old AniList titles without a MyAnimeList id cannot be refreshed; re-match them from the title page.
    Title.objects.filter(pk=title.pk).update(details_fetched=True)
    title.details_fetched = True
    return title


def refresh_title(title):
    """Full refresh used by the nightly job: details first, then scores if possible."""
    title = ensure_details(title)
    if settings.OMDB_API_KEY and title.imdb_id and title.scores_checked_at is None:
        title = fetch_omdb_scores(title)
    return title


def pause_for(title):
    """Polite gap between TMDB calls in long batches. Jikan calls pace themselves (see jikan_wait)."""
    if title.source == Title.SOURCE_TMDB:
        time.sleep(0.05)
