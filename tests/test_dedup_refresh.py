"""Duplicate detection and the nightly refresh endpoint."""

import pytest
from django.urls import reverse

from tracker import dedup, sources
from tracker.models import Entry, ImportBatch, ImportRow, Title

pytestmark = pytest.mark.django_db


def ghibli_pair(user):
    tmdb = Title.objects.create(source="tmdb", source_id="129", kind="movie", name="Spirited Away", year=2001)
    ani = Title.objects.create(
        source="anilist",
        source_id="199",
        kind="anime",
        name="Spirited Away",
        original_name="Sen to Chihiro no Kamikakushi",
        year=2001,
        mal_id="199",
    )
    return tmdb, ani


def test_same_name_and_year_across_sources_is_a_duplicate(user):
    tmdb, ani = ghibli_pair(user)
    assert dedup.find_duplicate("anilist", "199", "Spirited Away", year=2001) == tmdb
    assert dedup.find_duplicate("anilist", "199", "Spirited Away", year=1980) is None  # a remake decades apart is not


def test_shared_imdb_id_is_a_duplicate(user):
    a = Title.objects.create(source="tmdb", source_id="1", kind="movie", name="Heat", year=1995, imdb_id="tt0113277")
    assert dedup.find_duplicate("other", "x", "Completely different", imdb_id="tt0113277") == a


def test_merge_combines_entries_and_removes_the_copy(user):
    tmdb, ani = ghibli_pair(user)
    Entry.objects.create(user=user, title=tmdb, rating=5)
    Entry.objects.create(user=user, title=ani, status="planned", notes="rewatch with subtitles")
    batch = ImportBatch.objects.create(user=user, filename="a.csv")
    ImportRow.objects.create(batch=batch, raw_title="Spirited Away", state="matched", matched_title=ani)

    dedup.merge_titles(keep=tmdb, drop=ani)

    assert not Title.objects.filter(pk=ani.pk).exists()
    entry = Entry.objects.get(user=user)
    assert entry.title == tmdb and float(entry.rating) == 5 and entry.status == "watched"
    assert "rewatch with subtitles" in entry.notes
    tmdb.refresh_from_db()
    assert tmdb.mal_id == "199" and tmdb.original_name == "Sen to Chihiro no Kamikakushi"
    assert ImportRow.objects.get().matched_title == tmdb


def test_likely_pairs_page_and_merge(client_in, user):
    tmdb, ani = ghibli_pair(user)
    Entry.objects.create(user=user, title=tmdb)
    Entry.objects.create(user=user, title=ani)
    assert dedup.likely_pairs(user) == [(tmdb, ani)] or dedup.likely_pairs(user) == [(ani, tmdb)]
    page = client_in.get(reverse("duplicates"))
    assert b"Keep this one" in page.content
    client_in.post(reverse("merge_duplicate"), {"keep": ani.pk, "drop": tmdb.pk})
    assert list(Title.objects.values_list("pk", flat=True)) == [ani.pk]


def test_adding_a_twin_asks_first(client_in, user, fake_sources):
    tmdb = Title.objects.create(source="tmdb", source_id="900", kind="movie", name="Spirited Away", year=2001)
    Entry.objects.create(user=user, title=tmdb)
    form = {"source": "anilist", "kind": "anime", "source_id": "199", "status": "watched"}

    prompt = client_in.post(reverse("add_title"), form)
    assert b"Is this the same title?" in prompt.content
    assert not Title.objects.filter(source="anilist").exists()

    same = client_in.post(reverse("add_title"), {**form, "use_title": tmdb.pk})
    assert b"Added to Watched" in same.content
    assert Entry.objects.filter(user=user).count() == 1

    separate = client_in.post(reverse("add_title"), {**form, "confirm": "1"})
    assert b"Added to Watched" in separate.content
    assert Entry.objects.filter(user=user).count() == 2


def test_refresh_endpoint_needs_the_token(client, settings):
    settings.REFRESH_TOKEN = "s3cret"
    assert client.post(reverse("refresh_batch")).status_code == 403
    assert client.post(reverse("refresh_batch"), HTTP_X_REFRESH_TOKEN="wrong").status_code == 403
    settings.REFRESH_TOKEN = ""
    assert client.post(reverse("refresh_batch"), HTTP_X_REFRESH_TOKEN="").status_code == 403


def test_refresh_endpoint_fills_details_and_scores_within_budget(client, settings, monkeypatch):
    settings.REFRESH_TOKEN = "s3cret"
    settings.OMDB_API_KEY = "k"
    for i in range(3):
        Title.objects.create(source="tmdb", source_id=str(i), kind="movie", name=f"T{i}")

    def fake_details(title):
        title.imdb_id = f"tt{title.source_id}"
        title.details_fetched = True
        title.save()
        return title

    def fake_scores(title):
        title.imdb_rating = 7.0
        title.scores_checked_at = "2026-10-22T00:00:00Z"
        title.save()
        return title

    monkeypatch.setattr(sources, "fetch_tmdb_details", fake_details)
    monkeypatch.setattr(sources, "fetch_omdb_scores", fake_scores)
    monkeypatch.setattr(sources, "pause_for", lambda title: None)

    out = client.post(reverse("refresh_batch"), {"omdb_budget": "2"}, HTTP_X_REFRESH_TOKEN="s3cret").json()
    assert out["details_done"] == 3 and out["details_left"] == 0
    assert out["scores_done"] == 2 and out["scores_left"] == 1  # budget respected


def test_refresh_stops_cleanly_at_a_rate_limit(client, settings, monkeypatch):
    settings.REFRESH_TOKEN = "s3cret"
    Title.objects.create(source="tmdb", source_id="1", kind="movie", name="A")

    def limited(title):
        raise sources.RateLimited("TMDB rate limit reached")

    monkeypatch.setattr(sources, "fetch_tmdb_details", limited)
    out = client.post(reverse("refresh_batch"), HTTP_X_REFRESH_TOKEN="s3cret").json()
    assert out["stopped"] == "TMDB rate limit reached" and out["details_left"] == 1
