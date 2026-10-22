"""Seed Rio metro identities, without asserting municipal capabilities.

Rio de Janeiro already belongs to 0002's capital seed and is retained on reverse.
The other fifteen codes are introduced here. Once referenced, they require forward
repair rather than deleting the registry identity from under clients or rules.
"""

# CROSS_TENANT_OK: the reverse only runs a read-only existence check on
# clients_clientcompany.municipality_ibge_code across every firm, because the
# registry is global and a reference from any firm must refuse the delete; no tenant
# row is read beyond that boolean or written.

from typing import Final

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations
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
INSERTED_CODES: Final = tuple(code for code, _name in RIO_METRO if code != "3304557")


def seed(apps: app_registry.Apps, editor: BaseDatabaseSchemaEditor | None) -> None:
    """Idempotently register the IBGE identities; capabilities remain untouched."""
    municipality = apps.get_model("fiscal", "Municipality")
    alias = editor.connection.alias if editor is not None else "default"
    for code, name in RIO_METRO:
        municipality.objects.using(alias).update_or_create(
            ibge_code=code,
            defaults={"name": name, "uf": "RJ"},
        )


def unseed(apps: app_registry.Apps, editor: BaseDatabaseSchemaEditor | None) -> None:
    """Refuse referenced identities, then delete only this seed's new codes."""
    municipality = apps.get_model("fiscal", "Municipality")
    client = apps.get_model("clients", "ClientCompany")
    alias = editor.connection.alias if editor is not None else "default"
    if (
        client._base_manager.using(alias)
        .filter(municipality_ibge_code__in=INSERTED_CODES)
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
            .filter(**{f"{relation.field.name}__in": INSERTED_CODES})
            .exists()
        ):
            msg = (
                "Cannot reverse Rio metro seed: "
                f"{relation.related_model._meta.label} references a seeded municipality."
            )
            raise RuntimeError(msg)
    municipality.objects.using(alias).filter(ibge_code__in=INSERTED_CODES).delete()


def seed_from_global_registry() -> None:
    """Restore migration reference data after transactional test flushes."""
    seed(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [
        ("fiscal", "0002_seed_capitals"),
        ("clients", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
