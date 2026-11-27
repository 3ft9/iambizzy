from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bizzy", "0005_merge_duplicate_grants"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="account", name="one_google_grant_per_mode"),
        migrations.AlterField(
            model_name="account",
            name="identity",
            field=models.CharField(max_length=512, unique=True),
        ),
    ]
