from django.db import migrations, models


def merge_duplicate_grants(apps, schema_editor):
    """Keep one grant per Google account, preferring read/write, and move calendars to it."""
    db = schema_editor.connection.alias
    Account = apps.get_model("bizzy", "Account")
    Calendar = apps.get_model("bizzy", "Calendar")
    kept = {}
    # "write" sorts after "read", so the read/write grant is seen first.
    for account in Account.objects.using(db).order_by("-access", "pk"):
        keeper = kept.setdefault(account.identity, account)
        if keeper.pk != account.pk:
            Calendar.objects.using(db).filter(account=account).update(account=keeper)
            account.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bizzy", "0004_connection_labels"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_grants, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="account", name="one_google_grant_per_mode"),
        migrations.AlterField(
            model_name="account",
            name="identity",
            field=models.CharField(max_length=512, unique=True),
        ),
    ]
