from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse, HttpResponsePermanentRedirect
from django.templatetags.static import static
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="tracker/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("healthz", lambda request: HttpResponse("ok")),
    # Some browsers and bots ask for /favicon.ico regardless of the page's icon links.
    # Looked up on each request, not at import, so the hashed file name exists by then.
    path("favicon.ico", lambda request: HttpResponsePermanentRedirect(static("tracker/favicon-32.png"))),
    path("", include("tracker.urls")),
]
