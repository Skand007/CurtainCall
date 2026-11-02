import pytest

from tracker import sources
from tracker.models import Title


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


def test_tmdb_movie_result_is_parsed(settings):
    item = {
        "id": 949,
        "title": "Heat",
        "original_title": "Heat",
        "release_date": "1995-12-15",
        "poster_path": "/p.jpg",
        "overview": "Crime.",
        "genre_ids": [80, 18],
        "vote_average": 7.9,
    }
    c = sources.tmdb_candidate(item, Title.KIND_MOVIE)
    assert c["source_id"] == "949"
    assert c["year"] == 1995
    assert c["poster_url"] == settings.TMDB_IMAGE_BASE + "/p.jpg"
    assert c["animated"] is False


def test_tmdb_animation_genre_flags_animated():
    item = {"id": 920, "title": "Cars", "release_date": "2006-06-08", "genre_ids": [16, 10751]}
    assert sources.tmdb_candidate(item, Title.KIND_MOVIE)["animated"] is True


def test_missing_release_date_gives_no_year():
    item = {"id": 1, "name": "Unknown", "first_air_date": ""}
    assert sources.tmdb_candidate(item, Title.KIND_TV)["year"] is None


JIKAN_ITEM = {
    "mal_id": 199,
    "title": "Sen to Chihiro no Kamikakushi",
    "title_english": "Spirited Away",
    "title_synonyms": ["Chihiro"],
    "titles": [
        {"type": "Default", "title": "Sen to Chihiro no Kamikakushi"},
        {"type": "English", "title": "Spirited Away"},
    ],
    "type": "Movie",
    "episodes": 1,
    "duration": "2 hr 4 min",
    "aired": {"from": "2001-07-20T00:00:00+00:00"},
    "year": None,
    "genres": [{"name": "Adventure"}, {"name": "Award Winning"}, {"name": "Sci-Fi"}],
    "studios": [{"name": "Studio Ghibli"}],
    "score": 8.77,
    "images": {"jpg": {"large_image_url": "https://img/x.jpg"}},
    "synopsis": "A girl. [Written by MAL Rewrite]",
    "trailer": {"youtube_id": "ByXuk9QqQkk"},
}


def test_mal_item_is_parsed():
    c = sources.mal_candidate(JIKAN_ITEM)
    assert (c["source"], c["source_id"], c["kind"]) == ("mal", "199", "movie")
    assert c["name"] == "Spirited Away"
    assert c["original_name"] == "Sen to Chihiro no Kamikakushi"
    assert c["year"] == 2001  # from the airing date when MyAnimeList has no season year
    assert c["animated"] is True
    assert c["score"] == 8.77
    assert c["overview"] == "A girl."
    extra = c["extra"]
    assert extra["runtime"] == 124
    assert extra["studios"] == ["Studio Ghibli"]
    assert extra["genres"] == ["Adventure", "Science Fiction"]  # same spelling as TMDB, no award label
    assert extra["synonyms"] == ["Chihiro"]
    assert extra["release_date"] == "2001-07-20"
    assert extra["trailer_key"] == "ByXuk9QqQkk"
    assert extra["cast"] is None


def test_mal_types_become_films_or_series():
    assert sources.mal_kind("Movie", 1) == "movie"
    assert sources.mal_kind("TV", 12) == "tv"
    assert sources.mal_kind("TV", None) == "tv"
    assert sources.mal_kind("OVA", 1) == "movie"
    assert sources.mal_kind("CM", 3) == "tv"
    assert sources.mal_kind("ONA", None) == "tv"


def test_trailer_id_from_embed_link():
    assert (
        sources.youtube_id({"youtube_id": None, "embed_url": "https://www.youtube.com/embed/abc123?enablejsapi=1"})
        == "abc123"
    )
    assert sources.youtube_id(None) == ""


def test_duration_parsing():
    assert sources.parse_duration("24 min per ep") == 24
    assert sources.parse_duration("1 hr 55 min") == 115
    assert sources.parse_duration("1 hr") == 60
    assert sources.parse_duration("30 sec per ep") == 1
    assert sources.parse_duration("Unknown") is None


def test_genres_get_one_spelling():
    assert sources.clean_genres(["Action & Adventure", "Action", "Sci-Fi & Fantasy", "Suspense"]) == [
        "Action",
        "Adventure",
        "Science Fiction",
        "Fantasy",
        "Thriller",
    ]


def test_lead_studios_keep_the_first_and_major_studios_only():
    companies = [
        {"name": "Legendary Pictures"},
        {"name": "Some Finance Partners"},
        {"name": "Warner Bros. Pictures"},
        {"name": "Village Roadshow"},
    ]
    assert sources.lead_studios(companies) == ["Legendary Pictures", "Warner Bros. Pictures"]
    assert sources.lead_studios([{"name": "Tiny Films"}, {"name": "Another"}]) == ["Tiny Films"]
    assert sources.lead_studios([]) == []


def test_jikan_rate_limit_raises_after_one_retry(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    calls = []

    def get(*a, **k):
        calls.append(1)
        return FakeResponse({}, 429)

    monkeypatch.setattr(sources.session, "get", get)
    with pytest.raises(sources.RateLimited):
        sources.jikan_get("/anime", {"q": "x"})
    assert len(calls) == 2


def test_tmdb_without_token_raises(settings):
    settings.TMDB_TOKEN = ""
    with pytest.raises(sources.SourceError):
        sources.tmdb_get("/search/movie")


def test_tmdb_error_status_raises(settings, monkeypatch):
    settings.TMDB_TOKEN = "t"
    monkeypatch.setattr(sources.session, "get", lambda *a, **k: FakeResponse({}, 401))
    with pytest.raises(sources.SourceError):
        sources.tmdb_get("/search/movie")


def test_search_tmdb_ranks_year_matches_first(settings, monkeypatch):
    settings.TMDB_TOKEN = "t"
    payload = {
        "results": [
            {"id": 1, "title": "Aladdin", "release_date": "1992-11-25", "popularity": 90},
            {"id": 2, "title": "Aladdin", "release_date": "2019-05-24", "popularity": 50},
        ]
    }
    monkeypatch.setattr(sources.session, "get", lambda *a, **k: FakeResponse(payload))
    results = sources.search_tmdb("Aladdin", Title.KIND_MOVIE, year=2019)
    assert [r["year"] for r in results] == [2019, 1992]


@pytest.mark.django_db
def test_upsert_is_idempotent_and_updates():
    c = sources.mal_candidate({"mal_id": 47, "title": "Akira", "year": 1988, "type": "Movie", "episodes": 1})
    first = sources.upsert_title(c)
    c["name"] = "AKIRA"
    second = sources.upsert_title(c)
    assert first.pk == second.pk
    assert Title.objects.count() == 1
    assert Title.objects.get().name == "AKIRA"
    assert second.details_fetched is False  # search results lack the cast, so the full lookup is still due


@pytest.mark.django_db
def test_fetch_tmdb_details_fills_directors_and_runtime(settings, monkeypatch):
    settings.TMDB_TOKEN = "t"
    title = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", year=1995)
    payload = {
        "genres": [{"id": 80, "name": "Crime"}],
        "runtime": 170,
        "credits": {"crew": [{"job": "Director", "name": "Michael Mann"}, {"job": "Writer", "name": "X"}]},
    }
    monkeypatch.setattr(sources.session, "get", lambda *a, **k: FakeResponse(payload))
    sources.fetch_tmdb_details(title)
    title.refresh_from_db()
    assert title.directors == ["Michael Mann"]
    assert title.runtime_minutes == 170
    assert title.genres == ["Crime"]
    assert title.details_fetched
