from django.contrib import admin

from .models import Entry, ImportBatch, ImportRow, Title


@admin.register(Title)
class TitleAdmin(admin.ModelAdmin):
    list_display = ["name", "year", "kind", "source", "source_id", "details_fetched"]
    list_filter = ["kind", "source", "animated", "details_fetched"]
    search_fields = ["name", "original_name"]


@admin.register(Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "status", "rating", "watched_on"]
    list_filter = ["status", "title__kind"]
    search_fields = ["title__name"]
    raw_id_fields = ["title"]


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ["filename", "user", "hint", "created_at"]


@admin.register(ImportRow)
class ImportRowAdmin(admin.ModelAdmin):
    list_display = ["raw_title", "raw_year", "state", "matched_title", "batch"]
    list_filter = ["state", "batch"]
    search_fields = ["raw_title"]
    raw_id_fields = ["matched_title"]
