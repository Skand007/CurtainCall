#!/usr/bin/env bash
# Render build step: install, collect static files, migrate, make sure the owner account exists.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py ensure_admin
