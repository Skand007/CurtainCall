import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from tracker.models import Entry, ImportBatch, ImportRow, Title

pytestmark = pytest.mark.django_db


def upload(client, text, hint="movie", name="list.csv"):
    f = SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")
    return client.post(reverse("import_list"), {"file": f, "hint": hint})


def process_all(client, batch):
    for _ in range(20):
        resp = client.post(reverse("import_process", args=[batch.pk]), HTTP_HX_REQUEST="true")
        assert resp.status_code == 200
        if not batch.rows.filter(state=ImportRow.PENDING).exists():
            return resp
    raise AssertionError("import never finished")


def test_owner_pages_need_login(client):
    for name in ["search", "import_list", "change_password"]:
        resp = client.get(reverse(name))
        assert resp.status_code == 302
        assert reverse("login") in resp["Location"]


def test_old_library_link_sends_visitors_to_the_public_page(client, owner):
    resp = client.get(reverse("library") + "?kind=anime")
    assert resp.status_code == 302
    assert resp["Location"] == reverse("home") + "?kind=anime"


def test_visitors_see_the_owners_library_on_the_homepage(client, owner):
    heat = Title.objects.create(
        source="tmdb",
        source_id="949",
        kind="movie",
        name="Heat",
        year=1995,
        runtime_minutes=170,
        poster_url="https://img/heat.jpg",
    )
    Entry.objects.create(user=owner, title=heat, rating=4.5)
    resp = client.get(reverse("home"))
    assert resp.status_code == 200
    assert b"Skand Gupta" in resp.content
    assert b"Heat" in resp.content
    assert b"Owner login" in resp.content
    assert b"Add a title" not in resp.content  # owner tools stay hidden
    assert resp.context["hours"] == 3


def test_empty_site_shows_a_starting_page(client, db):
    resp = client.get(reverse("home"))
    assert resp.status_code == 200
    assert b"Log in to start" in resp.content


def test_title_page_shows_the_owners_take_but_not_private_notes(client, owner):
    heat = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", year=1995)
    Entry.objects.create(user=owner, title=heat, rating=4.5, notes="secret thoughts")
    resp = client.get(reverse("title", args=[heat.pk]))
    assert b"Skand Gupta's take" in resp.content
    assert "★★★★½".encode() in resp.content
    assert b"secret thoughts" not in resp.content


def test_change_password_keeps_you_logged_in(client, owner):
    client.force_login(owner)
    resp = client.post(
        reverse("change_password"),
        {
            "old_password": "pw-for-tests-123",
            "new_password1": "a-much-better-pass-42",
            "new_password2": "a-much-better-pass-42",
        },
    )
    assert resp.status_code == 302
    owner.refresh_from_db()
    assert owner.check_password("a-much-better-pass-42")
    assert client.get(reverse("search")).status_code == 200


def test_stars_filter():
    from tracker.templatetags.tracker_extras import stars

    assert stars(4.5) == "★★★★½"
    assert stars(1) == "★"
    assert stars(0.5) == "½"
    assert stars(None) == ""


def test_csv_import_matches_and_fills_library(client_in, user, fake_sources):
    resp = upload(client_in, "﻿Title,Year\nHeat,1995\nAladdin,\nLife of Pie,2012\nNo Such Film,1999\n")
    batch = ImportBatch.objects.get()
    assert resp.status_code == 302
    assert batch.rows.count() == 4

    last = process_all(client_in, batch)
    assert last["HX-Refresh"] == "true"

    states = dict(batch.rows.values_list("raw_title", "state"))
    assert states == {
        "Heat": ImportRow.MATCHED,
        "Aladdin": ImportRow.REVIEW,  # 1992 and 2019 both exist
        "Life of Pie": ImportRow.MATCHED,
        "No Such Film": ImportRow.NOT_FOUND,
    }
    names = set(Entry.objects.filter(user=user).values_list("title__name", flat=True))
    assert names == {"Heat", "Life of Pi"}
    assert Title.objects.get(name="Heat").runtime_minutes == 120  # details were fetched


def test_review_pick_adds_the_chosen_title(client_in, user, fake_sources):
    upload(client_in, "Title,Year\nAladdin,\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    row = batch.rows.get()
    index = next(i for i, c in enumerate(row.candidates) if c["year"] == 2019)
    resp = client_in.post(reverse("import_resolve", args=[row.pk]), {"action": "pick", "choice": index})
    assert resp.status_code == 200
    row.refresh_from_db()
    assert row.state == ImportRow.MATCHED
    assert Entry.objects.get(user=user).title.year == 2019


def test_review_retry_and_skip(client_in, user, fake_sources):
    upload(client_in, "Title,Year\nNo Such Film,\nAnother Missing,\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    missing, other = batch.rows.order_by("id")
    client_in.post(
        reverse("import_resolve", args=[missing.pk]),
        {"action": "retry", "title": "Heat", "year": "1995", "kind": "movie"},
    )
    client_in.post(reverse("import_resolve", args=[other.pk]), {"action": "skip"})
    missing.refresh_from_db()
    other.refresh_from_db()
    assert missing.state == ImportRow.MATCHED
    assert other.state == ImportRow.SKIPPED


def test_animated_list_searches_anilist_first(client_in, fake_sources):
    upload(client_in, "Title,Year\nSpirited Away,2001\nCars,2006\n", hint="animated")
    process_all(client_in, ImportBatch.objects.get())
    assert Title.objects.get(name="Spirited Away").source == "anilist"
    assert Title.objects.get(name="Cars").source == "tmdb"
    assert fake_sources[0][0] == "anilist"


def test_csv_without_title_column_is_rejected(client_in):
    resp = upload(client_in, "Film,Year\nHeat,1995\n")
    assert resp.status_code == 302
    assert ImportBatch.objects.count() == 0


def test_add_from_search_and_rate(client_in, user, fake_sources):
    resp = client_in.post(
        reverse("add_title"), {"source": "tmdb", "kind": "movie", "source_id": "949", "status": "planned"}
    )
    assert resp.status_code == 200
    entry = Entry.objects.get(user=user)
    assert entry.status == "planned"

    client_in.post(reverse("update_entry", args=[entry.pk]), {"status": "watched", "rating": "4.5"})
    entry.refresh_from_db()
    assert entry.status == "watched"
    assert float(entry.rating) == 4.5

    bad = client_in.post(reverse("update_entry", args=[entry.pk]), {"rating": "7"})
    assert bad.status_code == 400


def test_add_rejects_bad_reference(client_in):
    resp = client_in.post(reverse("add_title"), {"source": "imdb", "kind": "movie", "source_id": "tt123"})
    assert resp.status_code == 400


def test_cannot_edit_someone_elses_entry(client, user, django_user_model):
    title = Title.objects.create(source="tmdb", source_id="1", kind="movie", name="X")
    entry = Entry.objects.create(user=user, title=title)
    other = django_user_model.objects.create_user("other", password="pw-for-tests-456")
    client.force_login(other)
    resp = client.post(reverse("update_entry", args=[entry.pk]), {"status": "dropped"})
    assert resp.status_code == 404


def test_public_profile_and_stats(client, user):
    heat = Title.objects.create(
        source="tmdb",
        source_id="949",
        kind="movie",
        name="Heat",
        year=1995,
        genres=["Crime"],
        directors=["Michael Mann"],
        runtime_minutes=170,
    )
    akira = Title.objects.create(
        source="anilist",
        source_id="47",
        kind="anime",
        name="Akira",
        year=1988,
        animated=True,
        directors=["TMS"],
        runtime_minutes=124,
    )
    Entry.objects.create(user=user, title=heat, rating=4.5)
    Entry.objects.create(user=user, title=akira)

    resp = client.get(reverse("profile", args=["skand"]))
    assert resp.status_code == 200
    assert b"Heat" in resp.content and b"Akira" in resp.content

    filtered = client.get(reverse("profile", args=["skand"]), {"kind": "animated"}, HTTP_HX_REQUEST="true")
    assert b"Akira" in filtered.content and b"Heat" not in filtered.content

    stats = client.get(reverse("stats", args=["skand"]))
    assert stats.status_code == 200
    s = stats.context["stats"]
    assert s["total"] == 2
    assert s["animated"] == 1 and s["live_action"] == 1
    assert s["hours"] == 5  # 294 minutes
    assert s["directors"] == [("Michael Mann", 1)]


def test_delete_entry_marks_import_row_skipped(client_in, user, fake_sources):
    upload(client_in, "Title,Year\nHeat,1995\n")
    batch = ImportBatch.objects.get()
    process_all(client_in, batch)
    entry = Entry.objects.get(user=user)
    client_in.post(reverse("delete_entry", args=[entry.pk]))
    assert not Entry.objects.exists()
    assert batch.rows.get().state == ImportRow.SKIPPED
