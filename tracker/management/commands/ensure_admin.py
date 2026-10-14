"""Create the owner account from environment variables if it doesn't exist yet.

Used during deploys on hosts without a shell (e.g. Render's free tier):

    DJANGO_ADMIN_USERNAME=skand DJANGO_ADMIN_PASSWORD=... python manage.py ensure_admin
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create a superuser from DJANGO_ADMIN_USERNAME / DJANGO_ADMIN_PASSWORD if missing."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_ADMIN_USERNAME")
        password = os.environ.get("DJANGO_ADMIN_PASSWORD")
        if not username or not password:
            self.stdout.write("DJANGO_ADMIN_USERNAME/PASSWORD not set, skipping")
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"User {username} already exists")
            return
        User.objects.create_superuser(username=username, password=password, email="")
        self.stdout.write(self.style.SUCCESS(f"Created superuser {username}"))
