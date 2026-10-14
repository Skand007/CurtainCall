"""Fetch missing details (genres, directors, runtime) for TMDB titles.

python manage.py refresh_metadata            # only titles without details
python manage.py refresh_metadata --all      # refresh everything
"""

from django.core.management.base import BaseCommand

from tracker import sources
from tracker.models import Title


class Command(BaseCommand):
    help = "Fill in missing metadata from TMDB."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="refresh titles that already have details")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **options):
        qs = Title.objects.filter(source=Title.SOURCE_TMDB)
        if not options["all"]:
            qs = qs.filter(details_fetched=False)
        if options["limit"]:
            qs = qs[: options["limit"]]
        done, failed = 0, 0
        for title in qs:
            try:
                sources.fetch_tmdb_details(title)
                done += 1
            except (sources.SourceError, OSError) as exc:
                failed += 1
                self.stderr.write(f"{title}: {exc}")
        self.stdout.write(self.style.SUCCESS(f"Updated {done} titles, {failed} failed"))
