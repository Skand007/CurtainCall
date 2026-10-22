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


def test_anilist_media_is_parsed():
    media = {
        "id": 199,
        "title": {"english": "Spirited Away", "romaji": "Sen to Chihiro no Kamikakushi"},
        "synonyms": ["Chihiro"],
        "format": "MOVIE",
        "episodes": 1,
        "duration": 125,
        "seasonYear": 2001,
        "genres": ["Adventure"],
        "averageScore": 86,
        "coverImage": {"large": "https://img/x.jpg"},
        "studios": {"nodes": [{"name": "Studio Ghibli"}]},
        "description": "A girl.<br>",
    }
    c = sources.anilist_candidate(media)
    assert c["name"] == "Spirited Away"
    assert c["original_name"] == "Sen to Chihiro no Kamikakushi"
    assert c["score"] == 8.6
    assert c["extra"]["runtime"] == 125
    assert c["extra"]["studios"] == ["Studio Ghibli"]


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
    c = sources.anilist_candidate({"id": 5, "title": {"romaji": "Akira"}, "seasonYear": 1988})
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
