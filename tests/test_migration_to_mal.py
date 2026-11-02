"""The data migration from AniList to MyAnimeList and to Film/Series + Animated."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("tracker", "0004_mal_types_studios_locks")]
AFTER = [("tracker", "0005_switch_to_mal_and_types")]


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def test_anilist_titles_become_myanimelist_titles_keeping_entries():
    apps = migrate(BEFORE)
    User = apps.get_model("auth", "User")
    Title = apps.get_model("tracker", "Title")
    Entry = apps.get_model("tracker", "Entry")
    Batch = apps.get_model("tracker", "ImportBatch")
    Row = apps.get_model("tracker", "ImportRow")
    user = User.objects.create(username="skand")
    film = Title.objects.create(
        source="anilist",
        source_id="199",
        kind="anime",
        name="Spirited Away",
        year=2001,
        episodes=1,
        mal_id="199",
        directors=["Studio Ghibli"],
        genres=["Sci-Fi"],
    )
    show = Title.objects.create(
        source="anilist",
        source_id="16498",
        kind="anime",
        name="Attack on Titan",
        year=2013,
        episodes=25,
        mal_id="16498",
    )
    twin = Title.objects.create(
        source="anilist",
        source_id="999",
        kind="anime",
        name="Attack on Titan (dup)",
        year=2013,
        episodes=25,
        mal_id="16498",
    )
    lost = Title.objects.create(source="anilist", source_id="5", kind="anime", name="Obscure", episodes=3)
    heat = Title.objects.create(
        source="tmdb",
        source_id="949",
        kind="movie",
        name="Heat",
        year=1995,
        genres=["Action & Adventure"],
        details_fetched=True,
    )
    batch = Batch.objects.create(user=user, filename="a.csv")  # the import created the entries below
    Entry.objects.create(user=user, title=film, rating=5)
    Entry.objects.create(user=user, title=show, rating=4)
    Entry.objects.create(user=user, title=twin, notes="from the duplicate")
    Row.objects.create(
        batch=batch,
        raw_title="Spirited Away",
        state="matched",
        matched_title=film,
        candidates=[
            {
                "source": "anilist",
                "source_id": "199",
                "kind": "anime",
                "name": "Spirited Away",
                "extra": {"mal_id": "199", "format": "MOVIE", "episodes": 1},
            },
            {"source": "anilist", "source_id": "7", "kind": "anime", "name": "No MAL id", "extra": {}},
        ],
    )

    apps = migrate(AFTER)
    Title = apps.get_model("tracker", "Title")
    Entry = apps.get_model("tracker", "Entry")
    Row = apps.get_model("tracker", "ImportRow")

    film = Title.objects.get(pk=film.pk)
    assert (film.source, film.source_id, film.kind, film.animated) == ("mal", "199", "movie", True)
    assert film.studios == ["Studio Ghibli"] and film.directors == [] and film.genres == ["Science Fiction"]
    assert film.details_fetched is False
    show = Title.objects.get(pk=show.pk)
    assert (show.source, show.kind) == ("mal", "tv")
    assert not Title.objects.filter(pk=twin.pk).exists()  # folded into the first one
    combined = Entry.objects.get(title=show)
    assert float(combined.rating) == 4 and combined.notes == "from the duplicate"
    lost = Title.objects.get(pk=lost.pk)
    assert (lost.source, lost.kind, lost.details_fetched) == ("anilist", "tv", True)
    heat = Title.objects.get(pk=heat.pk)
    assert (heat.tmdb_type, heat.kind, heat.details_fetched) == ("movie", "movie", False)
    assert heat.genres == ["Action", "Adventure"]
    row = Row.objects.get()
    assert row.picked_title_ids == [film.pk] and len(row.created_entry_ids) == 1
    assert [(c["source"], c["source_id"], c["kind"]) for c in row.candidates] == [("mal", "199", "movie")]
