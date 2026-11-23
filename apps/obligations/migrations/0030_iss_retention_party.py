"""AXRM-046: add the ISS party with a persistent default for older writers."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cycles", "0011_actionproposal_redacted_at_and_more"),
        ("obligations", "0029_fiscal_register"),
    ]
    operations = [
        migrations.AddField(
            model_name="fiscaldocument",
            name="iss_retention_party",
            field=models.CharField(
                max_length=13,
                choices=[
                    ("nao_retido", "ISS não retido"),
                    ("tomador", "ISS retido pelo tomador"),
                    ("intermediario", "ISS retido pelo intermediário"),
                    ("desconhecido", "Retenção do ISS desconhecida"),
                ],
                default="desconhecido",
                db_default="desconhecido",
            ),
        ),
    ]
