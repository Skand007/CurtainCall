from django.templatetags.static import static

from .views import display_name, get_owner


def site(request):
    """Shared on every page: the owner, and link-preview defaults that pages can override."""
    owner = get_owner()
    name = display_name(owner) if owner else ""
    return {
        "site_owner": owner,
        "site_owner_name": name,
        "og_title": f"{name} · CurtainCall" if name else "CurtainCall",
        "og_description": (f"{name}'s" if name else "A") + " library of films and series, live action and animated, "
        "with ratings and stats.",
        "og_url": request.build_absolute_uri(request.path),
        "og_image": request.build_absolute_uri(static("tracker/og-card.jpg")),
        "og_image_is_card": True,
    }
