"""Seed Rio metro identities, without asserting municipal capabilities.

The upsert cannot tell afterwards which rows it created and which it found, so each
row it creates is recorded in `fiscal_municipalityseedrow`, and the reverse deletes
only those. A metro row registered before this migration (or Rio de Janeiro from the
capital seed) survives the reverse. Once referenced, an inserted row requires forward
repair rather than deleting the registry identity from under clients or rules.
"""

# CROSS_TENANT_OK: the reverse only runs a read-only existence check on
# clients_clientcompany.municipality_ibge_code across every firm, because the
# registry is global and a reference from any firm must refuse the delete; no tenant
# row is read beyond that boolean or written.

from typing import Final

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

RIO_METRO: Final = (
    ("3304557", "Rio de Janeiro"),
    ("3303302", "Niterói"),
    ("3304904", "São Gonçalo"),
    ("3301702", "Duque de Caxias"),
    ("3303500", "Nova Iguaçu"),
    ("3300456", "Belford Roxo"),
    ("3305109", "São João de Meriti"),
    ("3303203", "Nilópolis"),
    ("3302858", "Mesquita"),
    ("3304144", "Queimados"),
    ("3302270", "Japeri"),
    ("3302502", "Magé"),
    ("3301850", "Guapimirim"),
    ("3302007", "Itaguaí"),
    ("3305554", "Seropédica"),
    ("3303609", "Paracambi"),
)
SEED: Final = "fiscal.0003_seed_rio_metro"


def seed(apps: app_registry.Apps, editor: BaseDatabaseSchemaEditor | None) -> None:
    """Idempotently register the IBGE identities; capabilities remain untouched."""
    municipality = apps.get_model("fiscal", "Municipality")
    seed_row = apps.get_model("fiscal", "MunicipalitySeedRow")
    alias = editor.connection.alias if editor is not None else "default"
    for code, name in RIO_METRO:
        _row, created = municipality.objects.using(alias).update_or_create(
            ibge_code=code,
            defaults={"name": name, "uf": "RJ"},
        )
        if created:
            seed_row.objects.using(alias).update_or_create(
                ibge_code=code,
                defaults={"seed": SEED},
            )


def unseed(apps: app_registry.Apps, editor: BaseDatabaseSchemaEditor | None) -> None:
    """Refuse referenced identities, then delete only the rows this seed inserted."""
    municipality = apps.get_model("fiscal", "Municipality")
    seed_row = apps.get_model("fiscal", "MunicipalitySeedRow")
    client = apps.get_model("clients", "ClientCompany")
    alias = editor.connection.alias if editor is not None else "default"
    inserted = list(
        seed_row.objects.using(alias)
        .filter(seed=SEED)
        .values_list("ibge_code", flat=True),
    )
    if (
        client._base_manager.using(alias)
        .filter(municipality_ibge_code__in=inserted)
        .exists()
    ):
        msg = (
            "Cannot reverse Rio metro seed: a client references a seeded municipality."
        )
        raise RuntimeError(msg)
    # Protect all registered relations, including rule FKs when those migrations
    # exist, rather than allowing a CASCADE to remove somebody's later assessment.
    for relation in municipality._meta.related_objects:
        if (
            relation.related_model._base_manager.using(alias)
            .filter(**{f"{relation.field.name}__in": inserted})
            .exists()
        ):
            msg = (
                "Cannot reverse Rio metro seed: "
                f"{relation.related_model._meta.label} references a seeded municipality."
            )
            raise RuntimeError(msg)
    municipality.objects.using(alias).filter(ibge_code__in=inserted).delete()
    seed_row.objects.using(alias).filter(seed=SEED).delete()


def seed_from_global_registry() -> None:
    """Restore migration reference data after transactional test flushes."""
    seed(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [
        ("fiscal", "0002_seed_capitals"),
        ("clients", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MunicipalitySeedRow",
            fields=[
                (
                    "ibge_code",
                    models.CharField(
                        max_length=7,
                        primary_key=True,
                        serialize=False,
                        verbose_name="IBGE code",
                    ),
                ),
                ("seed", models.CharField(max_length=100, verbose_name="seed")),
            ],
            options={
                "verbose_name": "municipality seed row",
                "verbose_name_plural": "municipality seed rows",
            },
        ),
        migrations.RunPython(seed, unseed),
    ]
