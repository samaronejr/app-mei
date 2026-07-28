"""Seed the standard onboarding checklist.

The six rows are the anchor's list: tax profile, gov.br trust level, certificate
status, first DAS schedule, annual declaration status, invoice model. They live in a
table rather than in code so a Phase 3 connector can add its own item with an INSERT,
which is the entire reason the checklist is data-driven.

`clients_onboardingitemtemplate` is platform level and listed in
`apps.core.rls.NON_TENANT_TABLES`; no tenant-scoped row is written here, so the
migration needs no cross-tenant marker.

`update_or_create` rather than `create`: replaying this against a database that already
holds part of the list must be a no-op, not a duplicate-key failure.
"""

from typing import Final

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

TEMPLATES: Final[tuple[tuple[str, str], ...]] = (
    ("tax_profile", "Perfil fiscal completo"),
    ("govbr_trust_level", "Nível da conta gov.br registrado"),
    ("digital_certificate", "Situação do certificado digital registrada"),
    ("first_das_schedule", "Primeiro vencimento do DAS conhecido"),
    ("annual_declaration", "Situação da DASN-SIMEI conhecida"),
    ("invoice_model", "Modelo de emissão de nota configurado"),
)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Write the standard checklist rows, replacing any that drifted."""
    template_model = apps.get_model("clients", "OnboardingItemTemplate")
    for position, (key, label) in enumerate(TEMPLATES, start=1):
        template_model.objects.update_or_create(
            key=key,
            defaults={"label": label, "position": position, "is_active": True},
        )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Remove the standard checklist rows."""
    template_model = apps.get_model("clients", "OnboardingItemTemplate")
    template_model.objects.filter(key__in=[key for key, _label in TEMPLATES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0003_onboarding_checklist"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
