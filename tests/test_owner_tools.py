"""Owner tools: editing types, bulk edits, re-matching, undo, multi-select, click-through filters."""

import pytest
from django.urls import reverse

from tracker import matching
from tracker.models import Entry, ImportBatch, ImportRow, Title

from .conftest import cand
from .test_views import process_all, upload

pytestmark = pytest.mark.django_db


def make(name, year=2000, **kw):
    defaults = {"source": "tmdb", "source_id": name.replace(" ", "_") + str(year), "kind": "movie"}
    return Title.objects.create(name=name, year=year, **{**defaults, **kw})


@pytest.fixture
def owner_in(client, owner):
    client.force_login(owner)
    return client


# ---------------------------------------------------------------- editing types


def test_owner_changes_type_and_style_and_they_stay_locked(owner_in, owner):
    t = make("Spirited Away", 2001, kind="tv")
    Entry.objects.create(user=owner, title=t)
    owner_in.post(reverse("edit_title", args=[t.pk]), {"kind": "movie", "style": "animated", "studios": ""})
    t.refresh_from_db()
    assert (t.kind, t.animated) == ("movie", True)
    assert t.locked == ["animated", "kind"]  # studios were not changed, so not locked
    assert t.tmdb_type == "tv"  # TMDB's own type is never edited


def test_visitors_and_other_users_cannot_edit(client, owner, django_user_model):
    t = make("Heat", 1995)
    assert client.post(reverse("edit_title", args=[t.pk]), {"kind": "tv"}).status_code == 404
    other = django_user_model.objects.create_user("other", password="pw-for-tests-456")
    client.force_login(other)
    assert client.post(reverse("edit_title", args=[t.pk]), {"kind": "tv"}).status_code == 404
    page = client.get(reverse("title", args=[t.pk])).content
    assert b"Edit how this title is shown" not in page


def test_title_page_links_genres_studios_and_directors(owner_in, owner):
    t = make("Heat", 1995, genres=["Crime"], studios=["Regency"], directors=["Michael Mann"])
    Entry.objects.create(user=owner, title=t)
    page = owner_in.get(reverse("title", args=[t.pk])).content.decode()
    assert '/?genre=Crime"' in page
    assert '/?studio=Regency"' in page
    assert '/?director=Michael+Mann"' in page
    assert "Edit how this title is shown" in page


def test_bulk_edit_changes_only_ticked_titles_in_the_library(owner_in, owner):
    a, b, c = make("A"), make("B"), make("C")
    for t in (a, b):
        Entry.objects.create(user=owner, title=t)
    owner_in.post(reverse("bulk_edit"), {"title": [a.pk, c.pk], "style": "animated", "kind": "", "next": "/"})
    a.refresh_from_db(), b.refresh_from_db(), c.refresh_from_db()
    assert a.animated and not b.animated and not c.animated  # c is not in the library
    assert a.locked == ["animated"]


def test_select_mode_shows_checkboxes_for_the_owner_only(owner_in, owner, client):
    Entry.objects.create(user=owner, title=make("Heat", 1995))
    page = owner_in.get(reverse("home") + "?select=1").content.decode()
    assert 'form="bulk"' in page and 'id="bulk"' in page
    owner_in.logout()
    assert 'form="bulk"' not in client.get(reverse("home") + "?select=1").content.decode()


def test_rematch_moves_rating_to_the_right_title(owner_in, owner, fake_sources):
    wrong = make("Aladdin", 1992, source_id="812")
    Entry.objects.create(user=owner, title=wrong, rating=4, notes="loved it")
    resp = owner_in.post(
        reverse("rematch", args=[wrong.pk]), {"source": "tmdb", "kind": "movie", "source_id": "420817"}
    )
    right = Title.objects.get(source_id="420817")
    assert resp["Location"] == reverse("title", args=[right.pk])
    entry = Entry.objects.get(user=owner)
    assert entry.title == right and float(entry.rating) == 4 and entry.notes == "loved it"
    assert not Title.objects.filter(pk=wrong.pk).exists()


# ---------------------------------------------------------------- add: undo and several at once


def test_undo_an_add(client_in, user, fake_sources):
    client_in.post(reverse("add_title"), {"source": "tmdb", "kind": "movie", "source_id": "949"})
    entry = Entry.objects.get(user=user)
    resp = client_in.post(reverse("undo_add", args=[entry.pk]))
    assert b"Undone" in resp.content and b"Mark as watched" in resp.content
    assert not Entry.objects.exists()


def test_undo_never_removes_an_entry_with_a_rating(client_in, user):
    entry = Entry.objects.create(user=user, title=make("Heat", 1995), rating=4)
    client_in.post(reverse("undo_add", args=[entry.pk]))
    assert Entry.objects.filter(pk=entry.pk).exists()


def test_add_several_ticked_results(client_in, user, fake_sources):
    resp = client_in.post(
        reverse("add_many"), {"pick": ["tmdb|movie|949", "tmdb|movie|420817", "bogus"], "status": "planned"}
    )
    assert resp.status_code == 200
    assert sorted(Entry.objects.values_list("title__name", "status")) == [("Aladdin", "planned"), ("Heat", "planned")]
    assert resp.content.count(b"Undo") == 2


# ---------------------------------------------------------------- import: several picks, undo, download


def test_pick_several_candidates_then_undo(client_in, user, fake_sources):
    upload(client_in, "Title,Year\nAladdin,\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    row = batch.rows.get()
    client_in.post(reverse("import_resolve", args=[row.pk]), {"action": "pick", "choice": ["0", "1"]})
    row.refresh_from_db()
    assert row.state == ImportRow.MATCHED and len(row.picked_title_ids) == 2
    assert Entry.objects.filter(user=user).count() == 2

    client_in.post(reverse("import_resolve", args=[row.pk]), {"action": "undo"})
    row.refresh_from_db()
    assert row.state == ImportRow.REVIEW and row.candidates  # back up for review, choices intact
    assert not Entry.objects.filter(user=user).exists()


def test_undo_keeps_an_entry_that_was_already_in_the_library(client_in, user, fake_sources):
    heat = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", year=1995)
    Entry.objects.create(user=user, title=heat, rating=5)
    upload(client_in, "Title,Year\nHeat,1995\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    row = batch.rows.get()
    assert row.state == ImportRow.MATCHED and row.created_entry_ids == []
    client_in.post(reverse("import_resolve", args=[row.pk]), {"action": "undo", "compact": "1"})
    assert Entry.objects.filter(user=user, title=heat, rating=5).exists()


def test_automatic_matches_can_be_undone_too(client_in, user, fake_sources):
    upload(client_in, "Title,Year\nHeat,1995\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    page = client_in.get(reverse("import_detail", args=[batch.pk])).content.decode()
    assert "Matched and skipped rows (1)" in page
    row = batch.rows.get()
    client_in.post(reverse("import_resolve", args=[row.pk]), {"action": "undo", "compact": "1"})
    row.refresh_from_db()
    assert row.state == ImportRow.REVIEW and not Entry.objects.exists()


def test_unmatched_rows_download_as_csv(client_in, user, fake_sources):
    upload(client_in, "Title,Year\nHeat,1995\nNo Such Film,1999\nAladdin,\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    body = client_in.get(reverse("import_unmatched", args=[batch.pk])).content.decode("utf-8-sig")
    assert body.splitlines() == ["Title,Year,State", "No Such Film,1999,Not found", "Aladdin,,Needs review"]


def test_shorter_search_finds_candidates_but_never_matches_a_different_title(monkeypatch, user):
    def search_tmdb(query, kind="movie", year=None, limit=8):
        return [cand("Demon Slayer", 2020, source_id="1")] if query == "Demon Slayer" and kind == "movie" else []

    monkeypatch.setattr(matching.sources, "search_tmdb", search_tmdb)
    monkeypatch.setattr(matching.sources, "search_mal", lambda q, limit=8: [])
    batch = ImportBatch.objects.create(user=user, filename="x.csv")
    row = ImportRow.objects.create(batch=batch, raw_title="Demon Slayer: The Hashira Meeting Arc", raw_year=2020)
    matching.match_row(row, matching.HINT_MOVIE)
    assert row.state == ImportRow.REVIEW and row.candidates[0]["name"] == "Demon Slayer"


# ---------------------------------------------------------------- click-through filters


@pytest.fixture
def library(owner):
    titles = [
        make(
            "Heat",
            1995,
            genres=["Crime", "Drama"],
            directors=["Michael Mann"],
            studios=["Regency"],
            rt_critics=88,
            imdb_rating=8.3,
            runtime_minutes=170,
        ),
        make(
            "Collateral",
            2004,
            genres=["Crime"],
            directors=["Michael Mann"],
            studios=["DreamWorks Pictures"],
            rt_critics=86,
            imdb_rating=7.5,
        ),
        make(
            "Spirited Away",
            2001,
            source="mal",
            genres=["Fantasy"],
            studios=["Studio Ghibli"],
            animated=True,
            rt_critics=96,
            imdb_rating=8.6,
            runtime_minutes=125,
        ),
    ]
    for t in titles:
        Entry.objects.create(user=owner, title=t)
    return titles


@pytest.mark.parametrize(
    "query,expected",
    [
        ("decade=1990", {"Heat"}),
        ("decade=2000", {"Collateral", "Spirited Away"}),
        ("year=2001", {"Spirited Away"}),
        ("genre=Crime", {"Heat", "Collateral"}),
        ("director=Michael+Mann", {"Heat", "Collateral"}),
        ("studio=Studio+Ghibli", {"Spirited Away"}),
        ("critics=80", {"Heat", "Collateral"}),
        ("critics=90", {"Spirited Away"}),
        ("audience=80", {"Heat", "Spirited Away"}),
        ("style=animated", {"Spirited Away"}),
        ("kind=anime", {"Spirited Away"}),  # old links still work
        ("genre=Crime&decade=2000", {"Collateral"}),
        ("runtime=missing", {"Collateral"}),
    ],
)
def test_click_through_filters(client, library, query, expected):
    resp = client.get(reverse("home") + "?" + query)
    names = {e.title.name for e in resp.context["page"]}
    assert names == expected


def test_active_filters_show_as_removable_chips(client, library):
    page = client.get(reverse("home") + "?genre=Crime&decade=2000").content.decode()
    assert "2000s" in page and "Genre: Crime" in page
    assert 'href="?genre=Crime#library"' in page  # removing the decade keeps the genre
    assert '<h2 id="all-h" class="section-title">The library</h2>' in page
    assert '<option value="Fantasy"' in page  # genre dropdown lists the library's genres


def test_stats_link_to_filtered_library(client, owner, library):
    page = client.get(reverse("stats", args=[owner.username])).content.decode()
    assert '/?decade=1990"' in page
    assert '/?genre=Crime"' in page
    assert '/?director=Michael+Mann"' in page
    assert '/?studio=Studio+Ghibli"' in page
    assert '/?critics=80"' in page
    assert "/?runtime=missing&amp;status=watched" in page
    assert ">Studios<" in page and "Anime studios" not in page


# ---------------------------------------------------------------- refreshing from the browser


def test_owner_can_run_the_refresh(owner_in, monkeypatch):
    from tracker import sources

    Title.objects.create(source="tmdb", source_id="1", kind="movie", name="A")

    def fake_details(title):
        title.details_fetched = True
        title.save()
        return title

    monkeypatch.setattr(sources, "fetch_tmdb_details", fake_details)
    monkeypatch.setattr(sources, "pause_for", lambda title: None)
    assert b"1</strong> title waiting" in owner_in.get(reverse("refresh")).content
    resp = owner_in.post(reverse("refresh_run"))
    assert b"All titles are up to date" in resp.content


def test_import_pauses_and_carries_on_at_a_rate_limit(client_in, user, monkeypatch, fake_sources):
    from tracker import sources

    def limited(query, limit=8):
        raise sources.RateLimited("MyAnimeList allows 60 searches a minute; continuing shortly")

    monkeypatch.setattr(sources, "search_mal", limited)
    upload(client_in, "Title,Year\nSpirited Away,2001\n", hint="animated")
    batch = ImportBatch.objects.get()
    resp = client_in.post(reverse("import_process", args=[batch.pk]), HTTP_HX_REQUEST="true")
    page = resp.content.decode()
    assert "load delay:20s" in page and "carry on by itself" in page
    assert batch.rows.get().state == ImportRow.PENDING  # nothing lost; the row is tried again


def test_jikan_never_sleeps_long_inside_a_request(monkeypatch):
    from tracker import sources

    monkeypatch.setattr(sources, "_jikan_calls", [sources.time.monotonic()] * sources.JIKAN_PER_MINUTE)
    with pytest.raises(sources.RateLimited):
        sources.jikan_wait()
