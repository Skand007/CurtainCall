from django.conf import settings
from django.db import models


class Title(models.Model):
    """One film or series, cached from TMDB or MyAnimeList."""

    KIND_MOVIE = "movie"
    KIND_TV = "tv"
    KIND_CHOICES = [(KIND_MOVIE, "Film"), (KIND_TV, "Series")]

    SOURCE_TMDB = "tmdb"
    SOURCE_MAL = "mal"
    SOURCE_ANILIST = "anilist"  # old titles only; new ones come from TMDB or MyAnimeList
    SOURCE_CHOICES = [(SOURCE_TMDB, "TMDB"), (SOURCE_MAL, "MyAnimeList"), (SOURCE_ANILIST, "AniList")]

    # Fields the owner can set by hand. Once set, refreshes leave them alone.
    LOCKABLE = ["kind", "animated", "studios"]

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    source_id = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)  # Film or Series, as shown on the site
    tmdb_type = models.CharField(max_length=5, blank=True)  # "movie" or "tv": TMDB's own id space, never edited
    name = models.CharField(max_length=300)
    original_name = models.CharField(max_length=300, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    overview = models.TextField(blank=True)
    poster_url = models.URLField(blank=True)
    genres = models.JSONField(default=list, blank=True)
    directors = models.JSONField(default=list, blank=True)  # directors (films) or creators (series)
    studios = models.JSONField(default=list, blank=True)  # lead studios only
    locked = models.JSONField(default=list, blank=True)  # names of fields set by hand, see LOCKABLE
    runtime_minutes = models.PositiveIntegerField(null=True, blank=True)  # total runtime
    episodes = models.PositiveIntegerField(null=True, blank=True)
    animated = models.BooleanField(default=False)
    external_score = models.FloatField(null=True, blank=True)  # TMDB vote average or MyAnimeList score, both 0-10
    details_fetched = models.BooleanField(default=False)

    # Richer details, filled by the add flow and the nightly refresh.
    release_date = models.DateField(null=True, blank=True)
    backdrop_url = models.URLField(blank=True)  # wide image for banners
    cast = models.JSONField(default=list, blank=True)  # [{"name", "character", "photo"}], top billed first
    trailer_key = models.CharField(max_length=40, blank=True)  # YouTube video id
    box_office_usd = models.BigIntegerField(null=True, blank=True)  # worldwide gross from TMDB, films only

    # Cross-reference ids, used to spot the same title from different sources.
    imdb_id = models.CharField(max_length=15, blank=True, db_index=True)
    mal_id = models.CharField(max_length=15, blank=True, db_index=True)

    # Scores from OMDb (IMDb users, Rotten Tomatoes critics).
    imdb_rating = models.FloatField(null=True, blank=True)  # 0-10
    imdb_votes = models.PositiveIntegerField(null=True, blank=True)
    rt_critics = models.PositiveSmallIntegerField(null=True, blank=True)  # Tomatometer, 0-100
    scores_checked_at = models.DateTimeField(null=True, blank=True)  # last OMDb lookup, even if it found nothing

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "source_id"], name="unique_source_title"),
        ]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.year})" if self.year else self.name

    @property
    def source_url(self):
        if self.source == self.SOURCE_MAL:
            return f"https://myanimelist.net/anime/{self.source_id}"
        if self.source == self.SOURCE_ANILIST:
            return f"https://anilist.co/anime/{self.source_id}"
        return f"https://www.themoviedb.org/{self.tmdb_type or self.kind}/{self.source_id}"

    def save(self, *args, **kwargs):
        if self.source == self.SOURCE_TMDB and not self.tmdb_type:
            self.tmdb_type = self.kind  # a new TMDB title: its Film/Series is TMDB's own type
        super().save(*args, **kwargs)

    def is_locked(self, field):
        return field in (self.locked or [])

    @property
    def imdb_url(self):
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else ""

    @property
    def trailer_url(self):
        return f"https://www.youtube.com/watch?v={self.trailer_key}" if self.trailer_key else ""


class Entry(models.Model):
    """A title in a user's library."""

    STATUS_WATCHED = "watched"
    STATUS_WATCHING = "watching"
    STATUS_PLANNED = "planned"
    STATUS_DROPPED = "dropped"
    STATUS_CHOICES = [
        (STATUS_WATCHED, "Watched"),
        (STATUS_WATCHING, "Watching"),
        (STATUS_PLANNED, "Planned"),
        (STATUS_DROPPED, "Dropped"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="entries")
    title = models.ForeignKey(Title, on_delete=models.CASCADE, related_name="entries")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_WATCHED)
    rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)  # 0.5 - 5.0
    watched_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "title"], name="unique_user_title"),
            models.CheckConstraint(
                condition=models.Q(rating__isnull=True) | models.Q(rating__gte=0.5, rating__lte=5),
                name="rating_in_range",
            ),
        ]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.user} - {self.title}"


class ImportBatch(models.Model):
    """One uploaded CSV file."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="import_batches")
    filename = models.CharField(max_length=255)
    hint = models.CharField(max_length=10, default="movie")  # movie | animated
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename


class ImportRow(models.Model):
    """One row of an uploaded CSV, waiting to be matched to a Title."""

    PENDING = "pending"
    MATCHED = "matched"
    REVIEW = "review"
    NOT_FOUND = "not_found"
    SKIPPED = "skipped"
    STATE_CHOICES = [
        (PENDING, "Pending"),
        (MATCHED, "Matched"),
        (REVIEW, "Needs review"),
        (NOT_FOUND, "Not found"),
        (SKIPPED, "Skipped"),
    ]

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    raw_title = models.CharField(max_length=300)
    raw_year = models.PositiveSmallIntegerField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=PENDING)
    candidates = models.JSONField(default=list, blank=True)  # kept after a match so the choice can be undone
    matched_title = models.ForeignKey(Title, null=True, blank=True, on_delete=models.SET_NULL)  # first pick
    picked_title_ids = models.JSONField(default=list, blank=True)  # every title this row added, in order
    created_entry_ids = models.JSONField(default=list, blank=True)  # entries this row created; undo removes them

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.raw_title} ({self.raw_year or '?'})"
