from django.db import migrations

from apps.core.migrations._operations import EnableRLS


class Migration(migrations.Migration):
    dependencies = [
        ("core_tests", "0001_initial"),
    ]

    operations = [
        EnableRLS("ExampleTenantModel"),
    ]
