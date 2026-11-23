"""Drive the real rendered review and briefing forms without duplicating views."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlsplit

from django.urls import reverse

from apps.briefing.providers import isolated_source_items, source_item, source_items
from apps.briefing.source_rows import SourceContext
from apps.clients.models import ClientAssignment, ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.obligations.models import RevisionArtifact
from tests.cycles.test_playbook_http_conflicts import HiddenInputs
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.capture_fixtures import (
    START,
    capture_api,
    item,
    key,
    page,
    review_api,
    xml,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.test import Client
    from pytest_django.fixtures import SettingsWrapper

    from apps.briefing.types import BriefingItem
    from apps.cycles.records import ServiceCycle
    from apps.obligations.models import FiscalDocument
    from tests.integrations.capture_fixtures import CaptureWorld


def review_path(fiscal: FiscalDocument, cycle: ServiceCycle) -> str:
    return f"{reverse('document-review', args=[fiscal.pk])}?ciclo={cycle.pk}"


def hrefs(content: bytes, fiscal: FiscalDocument) -> list[str]:
    bare = reverse("document-review", args=[fiscal.pk])
    return [
        href
        for href in re.findall(r'<a\b[^>]*\shref="([^"]+)"', content.decode())
        if urlsplit(href).path == bare
    ]


def review_form(content: bytes, decision: str = "aceito") -> dict[str, str]:
    fields = HiddenInputs(content.decode()).values
    return {
        **{
            key: fields[key]
            for key in (
                "cycle_id",
                "revision_id",
                "expected_version",
                "expected_head_id",
                "input_digest",
            )
        },
        "decision": decision,
        "note": "Motivo sintético da correção.",
    }


def review_action(path: str, content: bytes) -> str:
    forms = re.finditer(
        r"<form\b([^>]*)>(.*?)</form>",
        content.decode(),
        re.DOTALL,
    )
    chosen = next(
        attrs
        for attrs, html in (m.groups() for m in forms)
        if "cycle_id" in HiddenInputs(html).values
    )
    match = re.search(r'\baction="([^"]+)"', chosen)
    assert match is not None
    return urljoin(path, str(match.group(1)))


def hoje_browser(world: CaptureWorld) -> Client:
    tenant = world.setup.graph.tenant
    tenant.features = [*tenant.features, "briefing_r1"]
    tenant.save(update_fields=["features"])
    staff = world.setup.graph.staff
    with tenant_context(world.tenant_id):
        ClientAssignment.objects.get_or_create(
            tenant_id=world.tenant_id,
            client_id=world.link.client_id,
            user=staff,
        )
    return world.setup.firm(staff)


def dismissal_form(content: bytes, fiscal: FiscalDocument) -> dict[str, str]:
    action = re.escape(reverse("briefing-dismiss"))
    forms = re.findall(
        rf'<form\b[^>]*\saction="{action}"[^>]*>.*?</form>',
        content.decode(),
        re.DOTALL,
    )
    (fields,) = (
        this
        for form in forms
        if (this := HiddenInputs(form).values).get("subject_ref") == str(fiscal.pk)
        and this["kind"] == "fiscal_document"
    )
    return {**fields, "reason": "Motivo sintético para ocultar este fato."}


def fiscal_item(world: CaptureWorld, fiscal: FiscalDocument) -> BriefingItem | None:
    """Require all three source paths to project the same exact fiscal fact."""
    with tenant_context(world.tenant_id):
        ctx = SourceContext(ClientCompany.objects.filter(pk=fiscal.client_id), START)
        combined = source_items(ctx)
        isolated, failures = isolated_source_items(ctx)
        single = source_item(ctx, "fiscal_document", fiscal.pk)
    expected = [] if single is None else [single]
    assert not failures
    for rows in (combined, isolated):
        assert [row for row in rows if row.kind == "fiscal_document"] == expected
    if single:
        assert len(single.fact_version) <= 128
    return single


def record_in(
    world: CaptureWorld,
    fiscal: FiscalDocument,
    cycle: ServiceCycle,
    decision: str = "rejeitado",
    head: FiscalDocumentReview | None = None,
) -> FiscalDocumentReview:
    api = review_api()
    with tenant_context(world.tenant_id):
        cycle.refresh_from_db()
        result = cast(
            "FiscalDocumentReview",
            api.record_fiscal_document_review(
                world.setup.graph.actor,
                cycle.pk,
                fiscal.pk,
                revision_id=fiscal.current_revision_id,
                decision=decision,
                input_digest=api.input_digest(fiscal),
                expected_version=cycle.version,
                expected_head_id=head.pk if head else None,
                note="Motivo sintético",
            ),
        )
        cycle.refresh_from_db()
        return result


def change_state(world: CaptureWorld, fiscal: FiscalDocument, state: str) -> None:
    with tenant_context(world.tenant_id):
        type(fiscal).objects.filter(pk=fiscal.pk).update(fiscal_state=state)
        fiscal.refresh_from_db()


def seed_legacy_decision(
    world: CaptureWorld,
    fiscal: FiscalDocument,
    revision: UUID,
    digest: str,
) -> FiscalDocumentReview:
    with tenant_context(world.tenant_id):
        return FiscalDocumentReview.objects.create(
            tenant_id=world.tenant_id,
            client_id=fiscal.client_id,
            document=fiscal,
            revision_id=revision,
            cycle=world.setup.cycle,
            reviewer=world.setup.graph.actor,
            decision="rejeitado",
            input_digest=digest,
        )


def retain_same_revision_cce(world: CaptureWorld, settings: SettingsWrapper) -> None:
    correction = (
        '<procEventoNFe xmlns="http://www.portalfiscal.inf.br/nfe">'
        f"<evento><infEvento><chNFe>{key(1)}</chNFe><tpEvento>110110</tpEvento>"
        "<nSeqEvento>2</nSeqEvento><detEvento><xCorrecao>Synthetic correction"
        "</xCorrecao></detEvento></infEvento></evento></procEventoNFe>"
    ).encode()
    replies = (
        page([item(1, full=True, cce=2)]),
        Reply(body=xml()),
        Reply(body=correction),
    )
    stream = world.stream()
    with FakeFocus(replies) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        capture_api().synchronize(world.tenant_id, stream.pk)
    with tenant_context(world.tenant_id):
        assert RevisionArtifact.objects.filter(role="xml_cce").exists()
