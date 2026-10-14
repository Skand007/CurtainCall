"""Data quality checks. Exits with status 1 if any check fails, so CI can gate on it.

python manage.py check_data
"""

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Exists, OuterRef, Q

from tracker.models import Entry, ImportRow, Title


def run_checks():
    """Return a list of (check name, number of bad rows, example) tuples."""
    this_year = datetime.date.today().year
    results = []

    dupes = Title.objects.values("source", "source_id").annotate(n=Count("id")).filter(n__gt=1)
    results.append(("titles unique per source id", dupes.count(), list(dupes[:3])))

    empty = Title.objects.filter(name="")
    results.append(("titles have a name", empty.count(), list(empty.values_list("pk", flat=True)[:3])))

    bad_year = Title.objects.filter(Q(year__lt=1880) | Q(year__gt=this_year + 3))
    results.append(("release years plausible", bad_year.count(), list(bad_year.values_list("name", "year")[:3])))

    bad_kind = Title.objects.filter(source=Title.SOURCE_ANILIST).exclude(kind=Title.KIND_ANIME)
    results.append(("AniList titles are anime", bad_kind.count(), list(bad_kind.values_list("name", flat=True)[:3])))

    bad_rating = Entry.objects.exclude(rating__isnull=True).filter(Q(rating__lt=0.5) | Q(rating__gt=5))
    results.append(("ratings between 0.5 and 5", bad_rating.count(), list(bad_rating.values_list("pk", flat=True)[:3])))

    half_steps = [e.pk for e in Entry.objects.exclude(rating__isnull=True) if (e.rating * 2) % 1 != 0]
    results.append(("ratings in half-star steps", len(half_steps), half_steps[:3]))

    future = Entry.objects.filter(watched_on__gt=datetime.date.today())
    results.append(("watch dates not in the future", future.count(), list(future.values_list("pk", flat=True)[:3])))

    orphan = ImportRow.objects.filter(state=ImportRow.MATCHED, matched_title__isnull=True)
    results.append(
        ("matched import rows point to a title", orphan.count(), list(orphan.values_list("pk", flat=True)[:3]))
    )

    in_library = Entry.objects.filter(user=OuterRef("batch__user"), title=OuterRef("matched_title"))
    missing = ImportRow.objects.filter(state=ImportRow.MATCHED, matched_title__isnull=False).exclude(Exists(in_library))
    results.append(
        ("matched imports are in the library", missing.count(), list(missing.values_list("raw_title", flat=True)[:3]))
    )

    return results


class Command(BaseCommand):
    help = "Run data quality checks on titles, entries and imports."

    def handle(self, *args, **options):
        failed = 0
        for name, bad, examples in run_checks():
            if bad:
                failed += 1
                self.stdout.write(self.style.ERROR(f"FAIL  {name}: {bad} bad rows, e.g. {examples}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"ok    {name}"))
        if failed:
            raise CommandError(f"{failed} data check(s) failed")
