import datetime

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tracker.models import Entry, ImportBatch, ImportRow, Title

pytestmark = pytest.mark.django_db


def test_clean_data_passes(user):
    title = Title.objects.create(source="tmdb", source_id="949", kind="movie", name="Heat", year=1995)
    Entry.objects.create(user=user, title=title, rating=4)
    call_command("check_data")


def test_implausible_year_fails(user):
    Title.objects.create(source="tmdb", source_id="1", kind="movie", name="Bad", year=1700)
    with pytest.raises(CommandError):
        call_command("check_data")


def test_future_watch_date_fails(user):
    title = Title.objects.create(source="tmdb", source_id="2", kind="movie", name="X", year=2000)
    Entry.objects.create(user=user, title=title, watched_on=datetime.date.today() + datetime.timedelta(days=3))
    with pytest.raises(CommandError):
        call_command("check_data")


def test_matched_import_missing_from_library_fails(user):
    title = Title.objects.create(source="tmdb", source_id="3", kind="movie", name="Y", year=2001)
    batch = ImportBatch.objects.create(user=user, filename="a.csv")
    ImportRow.objects.create(batch=batch, raw_title="Y", state=ImportRow.MATCHED, matched_title=title)
    with pytest.raises(CommandError):
        call_command("check_data")


def test_database_rejects_out_of_range_rating(user):
    from django.db import IntegrityError

    title = Title.objects.create(source="tmdb", source_id="4", kind="movie", name="Z")
    with pytest.raises(IntegrityError):
        Entry.objects.create(user=user, title=title, rating=9)
