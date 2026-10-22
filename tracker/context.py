from .views import display_name, get_owner


def site(request):
    owner = get_owner()
    return {"site_owner": owner, "site_owner_name": display_name(owner) if owner else ""}
