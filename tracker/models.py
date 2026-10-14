from django.conf import settings
from django.db import models


class Title(models.Model):
    """One film, series or anime, cached from TMDB or AniList."""

    KIND_MOVIE = "movie"
    KIND_TV = "tv"
    KIND_ANIME = "anime"
    KIND_CHOICES = [(KIND_MOVIE, "Movie"), (KIND_TV, "Series"), (KIND_ANIME, "Anime")]

    SOURCE_TMDB = "tmdb"
    SOURCE_ANILIST = "anilist"
    SOURCE_CHOICES = [(SOURCE_TMDB, "TMDB"), (SOURCE_ANILIST, "AniList")]

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    source_id = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    name = models.CharField(max_length=300)
    original_name = models.CharField(max_length=300, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    overview = models.TextField(blank=True)
    poster_url = models.URLField(blank=True)
    genres = models.JSONField(default=list, blank=True)
    directors = models.JSONField(default=list, blank=True)  # directors for films, studios for anime
    runtime_minutes = models.PositiveIntegerField(null=True, blank=True)  # total runtime
    episodes = models.PositiveIntegerField(null=True, blank=True)
    animated = models.BooleanField(default=False)
    external_score = models.FloatField(
        null=True, blank=True
    )  # TMDB vote avg (0-10) / AniList mean (0-100 scaled to 0-10)
    details_fetched = models.BooleanField(default=False)
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
        if self.source == self.SOURCE_ANILIST:
            return f"https://anilist.co/anime/{self.source_id}"
        return f"https://www.themoviedb.org/{self.kind}/{self.source_id}"


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
    candidates = models.JSONField(default=list, blank=True)
    matched_title = models.ForeignKey(Title, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.raw_title} ({self.raw_year or '?'})"
