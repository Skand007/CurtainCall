from django.db import migrations


def mark_for_refresh(apps, schema_editor):
    # Existing titles were fetched before cast, trailers, release dates and ids were stored.
    Title = apps.get_model("tracker", "Title")
    Title.objects.update(details_fetched=False)


class Migration(migrations.Migration):
    dependencies = [("tracker", "0002_richer_details")]
    operations = [migrations.RunPython(mark_for_refresh, migrations.RunPython.noop)]
