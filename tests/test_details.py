"""Richer details: TMDB cast/trailer/box office, AniList extras, OMDb scores, formatting."""

import datetime

import pytest

from tracker import sources
from tracker.models import Title
from tracker.stats import band_counts
from tracker.templatetags.tracker_extras import hours_minutes, money, short_count


class FakeResponse:
    def __init__(self, payload, status=200, text=""):
        self.payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self.payload


TMDB_MOVIE = {
    "genres": [{"id": 80, "name": "Crime"}, {"id": 18, "name": "Drama"}],
    "runtime": 170,
    "release_date": "1995-12-15",
    "revenue": 187436818,
    "imdb_id": "tt0113277",
    "backdrop_path": "/bd.jpg",
    "credits": {
        "crew": [{"job": "Director", "name": "Michael Mann"}],
        "cast": [
            {"name": "Robert De Niro", "character": "Neil McCauley", "profile_path": "/dn.jpg", "order": 1},
            {"name": "Al Pacino", "character": "Vincent Hanna", "profile_path": "/ap.jpg", "order": 0},
        ],
    },
    "videos": {
        "results": [
            {"site": "YouTube", "type": "Teaser", "key": "teaser1", "official": True},
            {"site": "YouTube", "type": "Trailer", "key": "trail2", "official": False},
            {"site": "YouTube", "type": "Trailer", "key": "trail3", "official": True},
        ]
    },
}


@pytest.mark.django_db
def test_tmdb_details_store_cast_trailer_box_office_and_ids(settings, monkeypatch):
    settings.TMDB_TOKEN = "t"
    title = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", year=1995)
    monkeypatch.setattr(sources.session, "get", lambda *a, **k: FakeResponse(TMDB_MOVIE))
    sources.fetch_tmdb_details(title)
    title.refresh_from_db()
    assert title.release_date == datetime.date(1995, 12, 15)
    assert title.box_office_usd == 187436818
    assert title.imdb_id == "tt0113277"
    assert title.trailer_key == "trail3"  # official trailer beats teaser and unofficial trailer
    assert [c["name"] for c in title.cast] == ["Al Pacino", "Robert De Niro"]  # billing order
    assert title.cast[0]["photo"].endswith("/w185/ap.jpg")
    assert title.backdrop_url.endswith("/w1280/bd.jpg")


@pytest.mark.django_db
def test_tmdb_unknown_revenue_is_not_zero_dollars(settings, monkeypatch):
    settings.TMDB_TOKEN = "t"
    title = Title.objects.create(source="tmdb", source_id="1", kind="movie", name="X")
    monkeypatch.setattr(sources.session, "get", lambda *a, **k: FakeResponse({**TMDB_MOVIE, "revenue": 0}))
    sources.fetch_tmdb_details(title)
    assert title.box_office_usd is None


def test_anilist_extras_are_parsed():
    media = {
        "id": 199,
        "idMal": 199,
        "title": {"english": "Spirited Away", "romaji": "Sen to Chihiro no Kamikakushi"},
        "startDate": {"year": 2001, "month": 7, "day": 20},
        "trailer": {"id": "ByXuk9QqQkk", "site": "youtube"},
        "bannerImage": "https://img/banner.jpg",
        "characters": {
            "edges": [
                {
                    "node": {"name": {"full": "Chihiro Ogino"}, "image": {"medium": "https://img/c.jpg"}},
                    "voiceActors": [{"name": {"full": "Rumi Hiiragi"}}],
                }
            ]
        },
    }
    extra = sources.anilist_candidate(media)["extra"]
    assert extra["mal_id"] == "199"
    assert extra["release_date"] == "2001-07-20"
    assert extra["trailer_key"] == "ByXuk9QqQkk"
    assert extra["backdrop_url"] == "https://img/banner.jpg"
    assert extra["cast"] == [{"name": "Rumi Hiiragi", "character": "Chihiro Ogino", "photo": "https://img/c.jpg"}]


def test_anilist_search_result_has_no_cast_so_updates_keep_it():
    extra = sources.anilist_candidate(
        {"id": 5, "title": {"romaji": "Akira"}, "trailer": {"id": "x", "site": "dailymotion"}}
    )["extra"]
    assert extra["cast"] is None
    assert extra["trailer_key"] == ""  # only YouTube trailers are linked


def test_parse_omdb():
    data = {
        "imdbRating": "8.3",
        "imdbVotes": "712,345",
        "Ratings": [
            {"Source": "Internet Movie Database", "Value": "8.3/10"},
            {"Source": "Rotten Tomatoes", "Value": "88%"},
        ],
    }
    assert sources.parse_omdb(data) == (8.3, 712345, 88)
    assert sources.parse_omdb({"imdbRating": "N/A", "imdbVotes": "N/A"}) == (None, None, None)


@pytest.mark.django_db
def test_omdb_scores_are_stored(settings, monkeypatch):
    settings.OMDB_API_KEY = "k"
    title = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", imdb_id="tt0113277")
    payload = {
        "Response": "True",
        "imdbRating": "8.3",
        "imdbVotes": "1,000",
        "Ratings": [{"Source": "Rotten Tomatoes", "Value": "88%"}],
    }
    monkeypatch.setattr(sources.session, "get", lambda *a, **k: FakeResponse(payload))
    sources.fetch_omdb_scores(title)
    title.refresh_from_db()
    assert (title.imdb_rating, title.rt_critics, title.imdb_votes) == (8.3, 88, 1000)
    assert title.scores_checked_at is not None


@pytest.mark.django_db
def test_omdb_daily_limit_raises_rate_limited(settings, monkeypatch):
    settings.OMDB_API_KEY = "k"
    title = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", imdb_id="tt0113277")
    monkeypatch.setattr(
        sources.session,
        "get",
        lambda *a, **k: FakeResponse(
            {"Response": "False", "Error": "Request limit reached!"}, 401, "Request limit reached!"
        ),
    )
    with pytest.raises(sources.RateLimited):
        sources.fetch_omdb_scores(title)
    title.refresh_from_db()
    assert title.scores_checked_at is None  # will be retried tomorrow


def test_score_bands():
    assert band_counts([0, 9, 10, 55, 90, 99, 100]) == [2, 1, 0, 0, 0, 1, 0, 0, 0, 3]


def test_formatting_helpers():
    assert money(187436818) == "$187.4 million"
    assert money(2923706026) == "$2.92 billion"
    assert money(None) == ""
    assert hours_minutes(170) == "2 h 50 min"
    assert hours_minutes(45) == "45 min"
    assert hours_minutes(120) == "2 h"
    assert short_count(712345) == "712K"
    assert short_count(1234567) == "1.2M"
