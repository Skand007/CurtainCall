from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("library/", views.library, name="library"),
    path("search/", views.search, name="search"),
    path("add/", views.add_title, name="add_title"),
    path("title/<int:pk>/", views.title_detail, name="title"),
    path("entry/<int:pk>/update/", views.update_entry, name="update_entry"),
    path("entry/<int:pk>/delete/", views.delete_entry, name="delete_entry"),
    path("import/", views.import_list, name="import_list"),
    path("import/<int:pk>/", views.import_detail, name="import_detail"),
    path("import/<int:pk>/process/", views.import_process, name="import_process"),
    path("import/row/<int:row_pk>/resolve/", views.import_resolve, name="import_resolve"),
    path("u/<str:username>/", views.profile, name="profile"),
    path("u/<str:username>/stats/", views.stats, name="stats"),
    path("account/password/", views.change_password, name="change_password"),
]
