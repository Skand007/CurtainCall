from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("library/", views.library, name="library"),
    path("search/", views.search, name="search"),
    path("add/", views.add_title, name="add_title"),
    path("add/many/", views.add_many, name="add_many"),
    path("add/undo/<int:pk>/", views.undo_add, name="undo_add"),
    path("title/<int:pk>/", views.title_detail, name="title"),
    path("title/<int:pk>/edit/", views.edit_title, name="edit_title"),
    path("title/<int:pk>/rematch/", views.rematch, name="rematch"),
    path("library/edit/", views.bulk_edit, name="bulk_edit"),
    path("entry/<int:pk>/update/", views.update_entry, name="update_entry"),
    path("entry/<int:pk>/delete/", views.delete_entry, name="delete_entry"),
    path("import/", views.import_list, name="import_list"),
    path("export.csv", views.export_library, name="export_library"),
    path("import/<int:pk>/", views.import_detail, name="import_detail"),
    path("import/<int:pk>/process/", views.import_process, name="import_process"),
    path("import/<int:pk>/unmatched.csv", views.import_unmatched, name="import_unmatched"),
    path("import/row/<int:row_pk>/resolve/", views.import_resolve, name="import_resolve"),
    path("u/<str:username>/", views.profile, name="profile"),
    path("u/<str:username>/stats/", views.stats, name="stats"),
    path("account/password/", views.change_password, name="change_password"),
    path("duplicates/", views.duplicates, name="duplicates"),
    path("duplicates/merge/", views.merge_duplicate, name="merge_duplicate"),
    path("tasks/refresh/", views.refresh_batch, name="refresh_batch"),
    path("refresh/", views.refresh_page, name="refresh"),
    path("refresh/run/", views.refresh_run, name="refresh_run"),
]
