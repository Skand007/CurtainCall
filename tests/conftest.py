import pytest
from django.contrib.auth import get_user_model

from tracker import sources


def cand(name, year, source="tmdb", source_id=None, kind="movie", original="", synonyms=None, animated=False):
    """Build a candidate dict like the source clients return."""
    return {
        "source": source,
        "source_id": source_id or f"{name}-{year}".replace(" ", "_"),
        "kind": kind,
        "name": name,
        "original_name": original,
        "year": year,
        "poster_url": "",
        "overview": "",
        "animated": animated,
        "score": 7.0,
        "popularity": 1,
        "extra": {
            "synonyms": synonyms or [],
            "genres": ["Animation"],
            "studios": ["Studio X"],
            "episodes": 1,
            "runtime": 100,
        }
        if source == "anilist"
        else {},
    }


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("skand", password="pw-for-tests-123")


@pytest.fixture
def client_in(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def fake_sources(monkeypatch):
    """Replace every network call with an in-memory catalogue."""
    catalogue = {
        "movie": [
            cand("Aladdin", 1992, source_id="812", animated=True),
            cand("Aladdin", 2019, source_id="420817"),
            cand("Heat", 1995, source_id="949"),
            cand("Life of Pi", 2012, source_id="87827"),
            cand("Cars", 2006, source_id="920", animated=True),
        ],
        "tv": [cand("Fargo", 2014, source_id="60622", kind="tv")],
        "anilist": [
            cand(
                "Spirited Away",
                2001,
                source="anilist",
                source_id="199",
                kind="anime",
                original="Sen to Chihiro no Kamikakushi",
                animated=True,
            ),
        ],
    }
    calls = []

    def find(pool, query):
        q = query.lower()
        return [c for c in pool if q[:4] in c["name"].lower() or q[:4] in c["original_name"].lower()]

    def search_tmdb(query, kind="movie", year=None, limit=8):
        calls.append(("tmdb", kind, query))
        return [dict(c) for c in find(catalogue[kind], query)][:limit]

    def search_anilist(query, limit=8):
        calls.append(("anilist", query))
        return [dict(c) for c in find(catalogue["anilist"], query)][:limit]

    def get_candidate(source, kind, source_id):
        pool = catalogue["anilist"] if source == "anilist" else catalogue[kind]
        for c in pool:
            if c["source_id"] == source_id:
                return dict(c)
        raise sources.SourceError("not found")

    def fetch_tmdb_details(title):
        title.genres = ["Drama"]
        title.directors = ["Someone"]
        title.runtime_minutes = 120
        title.details_fetched = True
        title.save()
        return title

    monkeypatch.setattr(sources, "search_tmdb", search_tmdb)
    monkeypatch.setattr(sources, "search_anilist", search_anilist)
    monkeypatch.setattr(sources, "get_candidate", get_candidate)
    monkeypatch.setattr(sources, "fetch_tmdb_details", fetch_tmdb_details)
    return calls
