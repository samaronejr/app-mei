"""The `pgmei` qualification scenario (AXRM-017; PQ-SERPRO-3), never a live path.

A fresh synthetic firm accepts its termo, then asks for one MEI DAS guide through
the ordinary staff service: the same protocol, reservation, decision-before-finality
and private, quarantined storage. Production stays unregistered (todo 40).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from time import monotonic
from typing import TYPE_CHECKING

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from apps.clients.models import ClientStatus, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink
from apps.cycles.models import PlaybookVersion
from apps.cycles.packs import install_system_packs
from apps.cycles.services.cycle import open_cycle
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import ClientProviderLink
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.pgmei import (
    GuideRefused,
    fake_origin,
    obtain_das_guide,
)
from apps.integrations.serpro.qualification import (
    QualificationEvidence,
    Scenario,
    SyntheticTermo,
    register_scenario,
    synthetic_termo,
)
from apps.obligations.models import Document

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.integrations.transport_types import Environment, JSONValue

# Enough for one emission at any plausible tariff; refused, never extended, beyond it.
SYNTHETIC_CAP_MICROBRL = 5_000_000


@contextmanager
def _guide_settings(environment: Environment) -> Iterator[None]:
    caps = dict(settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL)
    caps.setdefault("serpro", "5.00")
    prices = dict(settings.USAGE_UNIT_PRICES_BRL)
    if environment == "fake":
        # A fake emission costs nothing real; trial uses the configured tariff.
        prices.setdefault("serpro_emissao", "0.00")
    with override_settings(
        PROVIDERS_PAUSED=False,
        SERPRO_PAUSED=False,
        SERPRO_ENVIRONMENT=environment,
        PROVIDER_GLOBAL_MONTHLY_CAP_BRL=caps,
        USAGE_UNIT_PRICES_BRL=prices,
    ):
        yield


def _mei_cycle(world: SyntheticTermo) -> tuple[ClientProviderLink, UUID]:
    competence = timezone.localdate().replace(day=1)
    with tenant_context(world.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=SYNTHETIC_CAP_MICROBRL)
        install_system_packs(world.tenant)
        world.client.status = ClientStatus.ACTIVE
        world.client.save(update_fields=["status"])
        engagement = Engagement.objects.create(
            tenant=world.tenant,
            client=world.client,
            service_kind="mei_mensal",
            starts_on=date(competence.year, 1, 1),
            responsible=world.actor,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=world.tenant,
            client=world.client,
            regime="mei",
            valid_from=date(competence.year, 1, 1),
            provenance="staff_entry",
        )
        link = ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=world.client,
            connection=world.connection,
            provider_company_ref="qualificacao-pgmei",
            status="ativa",
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        cycle = open_cycle(
            world.actor,
            engagement.pk,
            competence,
            definition.pk,
            idempotency_key="qualificacao-pgmei",
        )
    return link, cycle.id


def qualify_pgmei(environment: Environment) -> QualificationEvidence:
    """Run termo then one guide request; leave the synthetic firm inactive."""
    origin = fake_origin() if environment == "fake" else ""
    if environment == "fake" and not origin:
        msg = "origem_fake_ausente"
        raise ValueError(msg)
    world = synthetic_termo(environment)
    start = monotonic()
    code = ""
    try:
        with _guide_settings(environment):
            submit_termo(
                world.actor,
                world.tenant.pk,
                TermoSubmission(
                    world.connection.pk,
                    world.document.pk,
                    world.connection.version,
                    world.parties,
                    origin,
                ),
            )
            _, cycle_id = _mei_cycle(world)
            try:
                code = obtain_das_guide(
                    world.actor, world.tenant.pk, cycle_id, origin=origin
                ).code
            except GuideRefused as refused:
                code = refused.code
        elapsed = monotonic() - start
        with tenant_context(world.tenant.pk):
            call = ProviderCall.objects.filter(operation="SP-DAS").first()
            document = (
                Document.objects.filter(acquisition_receipt_ref=str(call.pk)).first()
                if call is not None
                else None
            )
            intake = (
                DocumentIntake.objects.filter(document=document).first()
                if document is not None
                else None
            )
            fields: dict[str, JSONValue] = {
                "outcome": code,
                "call_state": call.state if call is not None else "",
                "guide_stored": document is not None,
                "guide_private": document is not None and not document.portal_visible,
                "guide_bytes": document.byte_size if document is not None else 0,
                "intake_state": intake.state if intake is not None else "",
                "linked": document is not None
                and EvidenceLink.objects.filter(document=document).exists(),
            }
    finally:
        world.tenant.is_active = False
        world.tenant.save(update_fields=["is_active"])
    return QualificationEvidence(
        call.request_digest if call is not None else "",
        call.response_digest if call is not None else "",
        call.http_status if call is not None else None,
        "emissao",
        fields,
        elapsed,
    )


def register() -> None:
    """Register once at app startup, beside the termo scenario."""
    register_scenario(
        Scenario("serpro", "pgmei", frozenset({"fake", "trial"}), qualify_pgmei)
    )
