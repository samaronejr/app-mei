"""Seed the 27 state capitals and their capability rows.

The capitals are the smallest seed that is useful on day one: they are where most MEI
clients are, and they give the registry enough rows for the variance to be visible
rather than theoretical.

Every capability row here is marked `verified_on = None` and carries a note saying so.
That is deliberate and is the honest state of this data: the national NFS-e adherence
list published at gov.br/nfse now covers every federated entity, but whether a given
municipality still requires a digital certificate for its own portal is a per-city
question that nobody on this project has checked city by city. Writing a confident
`verified_on` date would turn an unverified default into something a reader would trust.

The capability defaults are therefore the conservative pair: the national emitter is
NOT claimed, and a certificate IS assumed to be required, until a human verifies
otherwise and fills in `verified_on`.
"""

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

# IBGE code, name, UF. The first two digits of every code are the state's own IBGE
# code, which the registry pack asserts for all 27 rows rather than trusting.
CAPITALS = (
    ("1200401", "Rio Branco", "AC"),
    ("2704302", "Maceió", "AL"),
    ("1600303", "Macapá", "AP"),
    ("1302603", "Manaus", "AM"),
    ("2927408", "Salvador", "BA"),
    ("2304400", "Fortaleza", "CE"),
    ("5300108", "Brasília", "DF"),
    ("3205309", "Vitória", "ES"),
    ("5208707", "Goiânia", "GO"),
    ("2111300", "São Luís", "MA"),
    ("5103403", "Cuiabá", "MT"),
    ("5002704", "Campo Grande", "MS"),
    ("3106200", "Belo Horizonte", "MG"),
    ("1501402", "Belém", "PA"),
    ("2507507", "João Pessoa", "PB"),
    ("4106902", "Curitiba", "PR"),
    ("2611606", "Recife", "PE"),
    ("2211001", "Teresina", "PI"),
    ("3304557", "Rio de Janeiro", "RJ"),
    ("2408102", "Natal", "RN"),
    ("4314902", "Porto Alegre", "RS"),
    ("1100205", "Porto Velho", "RO"),
    ("1400100", "Boa Vista", "RR"),
    ("4205407", "Florianópolis", "SC"),
    ("3550308", "São Paulo", "SP"),
    ("2800308", "Aracaju", "SE"),
    ("1721000", "Palmas", "TO"),
)

UNVERIFIED_NOTE = (
    "Seeded from the IBGE municipality list. Capability flags are conservative "
    "defaults and have not been verified for this municipality; verified_on is null "
    "until a human checks."
)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Insert the capitals and a conservative capability row for each.

    Idempotent, because the test suite replays it after a transactional test has
    truncated the tables, and because a re-run of migrate must not fail.
    """
    municipality_model = apps.get_model("fiscal", "Municipality")
    capability_model = apps.get_model("fiscal", "MunicipalityCapability")

    for ibge_code, name, uf in CAPITALS:
        municipality, _created = municipality_model.objects.update_or_create(
            ibge_code=ibge_code,
            defaults={"name": name, "uf": uf},
        )
        capability_model.objects.get_or_create(
            municipality=municipality,
            defaults={
                "nfse_national_emitter": False,
                "requires_certificate": True,
                "notes": UNVERIFIED_NOTE,
                "verified_on": None,
            },
        )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Remove exactly the rows this migration inserted, and nothing else."""
    municipality_model = apps.get_model("fiscal", "Municipality")
    municipality_model.objects.filter(
        ibge_code__in=[code for code, _name, _uf in CAPITALS],
    ).delete()


def seed_from_global_registry() -> None:
    """Re-seed using the live app registry, for the test suite's restore fixture.

    A transactional test truncates every table and pytest-django does not replay
    `RunPython`, which would leave the registry empty for whatever test ran next. The
    suite calls this rather than reimplementing the seed, so the two can never disagree.
    """
    seed(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [("fiscal", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
