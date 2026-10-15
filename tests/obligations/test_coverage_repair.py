"""B2/B5/B6: real rendered commands and populated, source-aware readers."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils import timezone

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, EvidenceLink
from apps.obligations.coverage import coverage_for
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from apps.obligations.observations import record_staff_fact
from apps.tenants.models import Membership, TenantRole
from tests.cycles import observation_fixtures
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.document_fixtures import reviewed, submit
from tests.cycles.observation_fixtures import (
    coverage_item,
    payment_claim,
    payment_subject,
    staff_counts,
    staff_post,
)
from tests.portal.request_fixtures import PortalSetup
from tests.ui.factories import add_member
from tests.ui.refusal_contract import assert_readable_page

receipt_case = observation_fixtures.receipt_case
pytestmark = pytest.mark.django_db(transaction=True)
CLOCK = datetime(2026, 10, 7, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(-1, "verified"), (0, "stale"), (1, "stale")],
    ids=["before", "at", "after"],
)
def test_official_api_expires_at_the_half_open_boundary(
    receipt_case: PortalSetup, offset: int, expected: str
) -> None:
    observed = CLOCK - timedelta(days=7)
    payment_claim(
        payment_subject(receipt_case),
        source="official_api",
        verification="verified",
        observed_at=observed,
        outcome="accepted",
    )

    result = coverage_item(receipt_case, now=CLOCK + timedelta(microseconds=offset))

    assert result.state == expected
    assert result.last_success == observed


def test_file_import_has_no_invented_temporal_expiry(
    receipt_case: PortalSetup,
) -> None:
    payment_claim(
        payment_subject(receipt_case),
        source="official_file_import",
        verification="verified",
        observed_at=CLOCK - timedelta(days=90),
        outcome="accepted",
    )

    assert coverage_item(receipt_case, now=CLOCK).state == "verified"


def test_explicit_manual_staleness_survives_both_readers(
    receipt_case: PortalSetup,
) -> None:
    payment_claim(payment_subject(receipt_case), verification="stale")

    assert coverage_item(receipt_case).state == "stale"
    response = receipt_case.portal().get("/pagamentos/")
    assert response.status_code == 200
    assert 'data-payment-state="stale"' in response.content.decode()
    assert 'data-payment-state="manual"' not in response.content.decode()


def test_failed_attempt_keeps_the_last_good_claim_source_and_clock(
    receipt_case: PortalSetup,
) -> None:
    subject = payment_subject(receipt_case)
    good = payment_claim(subject, observed_at=CLOCK - timedelta(days=1))
    payment_claim(
        subject,
        source="official_api",
        verification="refresh_failed",
        outcome="refresh_failed",
        observed_at=CLOCK,
    )

    result = coverage_item(receipt_case, now=CLOCK)
    response = receipt_case.portal().get("/pagamentos/")

    assert result.last_success == good.observed_at
    assert result.head_id == good.pk
    assert result.head_source == "staff_recorded"
    assert result.state == "manual"
    assert result.covered_by["payment"].pk == good.pk
    assert response.status_code == 200
    assert 'data-payment-state="manual"' in response.content.decode()
    assert 'data-payment-state="stale"' not in response.content.decode()


def test_failed_attempt_alone_has_no_last_success(receipt_case: PortalSetup) -> None:
    payment_claim(
        payment_subject(receipt_case),
        source="official_api",
        verification="refresh_failed",
        outcome="refresh_failed",
    )

    result = coverage_item(receipt_case)

    assert result.state == "refresh_failed"
    assert result.last_success is None


@pytest.mark.parametrize("nullable", [False, True], ids=["exact", "nullable"])
def test_populated_wrong_period_cannot_win_on_the_portal(
    receipt_case: PortalSetup, nullable: bool
) -> None:
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        SourceObservation.objects.create(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_ref=subject,
            kind="payment",
            period=None if nullable else PERIOD,
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=timezone.now() - timedelta(days=1),
        )
        SourceObservation.objects.create(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_ref=subject,
            kind="payment",
            period=date(2026, 10, 1),
            source_kind="official_api",
            verification_status="verified",
            observed_at=timezone.now(),
        )

    response = receipt_case.portal().get("/pagamentos/")

    assert response.status_code == 200
    assert 'data-payment-state="manual"' in response.content.decode()
    assert 'data-payment-state="verified"' not in response.content.decode()


@pytest.mark.parametrize("foreign_identity", ["period", "kind"])
def test_unrelated_successor_cannot_hide_a_current_claim(
    receipt_case: PortalSetup, foreign_identity: str
) -> None:
    subject = payment_subject(receipt_case)
    good = payment_claim(subject)
    with tenant_context(subject.tenant_id):
        SourceObservation.objects.create(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_ref=subject,
            kind="guide_issued" if foreign_identity == "kind" else "payment",
            period=date(2026, 10, 1) if foreign_identity == "period" else PERIOD,
            source_kind="staff_recorded",
            verification_status="unsupported",
            observed_at=timezone.now(),
            supersedes=good,
        )

    response = receipt_case.portal().get("/pagamentos/")

    assert coverage_item(receipt_case).head_id == good.pk
    assert 'data-payment-state="manual"' in response.content.decode()


def test_expired_official_row_is_not_green_on_the_portal(
    receipt_case: PortalSetup,
) -> None:
    payment_claim(
        payment_subject(receipt_case),
        source="official_api",
        verification="verified",
        observed_at=timezone.now() - timedelta(days=8),
        outcome="accepted",
    )

    response = receipt_case.portal().get("/pagamentos/")

    assert response.status_code == 200
    assert 'data-payment-state="stale"' in response.content.decode()
    assert "badge-ok" not in response.content.decode()


def test_calendar_assertion_is_neutral_and_uses_the_persisted_clock(
    receipt_case: PortalSetup,
) -> None:
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        assertion = SourceObservation.objects.get(
            subject_ref=subject, kind="calendar_scheduled"
        )

    assert assertion.source_kind == "deterministic_rule"
    assert assertion.verification_status == "unknown"
    assert assertion.observed_at == subject.created_at
    assert coverage_item(receipt_case).facets["calendar"] == "unknown"


def test_coverage_service_denies_unassigned_staff_with_assigned_control(
    receipt_case: PortalSetup,
) -> None:
    graph = receipt_case.graph
    with tenant_context(graph.tenant.pk):
        with pytest.raises(Http404):
            coverage_for(graph.staff, graph.client, PERIOD)
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        assert coverage_for(graph.staff, graph.client, PERIOD)[0].obligation_id == (
            receipt_case.request.obligation_id
        )


def test_stale_rendered_staff_revision_writes_nothing(
    receipt_case: PortalSetup,
) -> None:
    before = staff_counts(receipt_case)

    response = staff_post(
        receipt_case, expected_version=str(receipt_case.cycle.version - 1)
    )

    assert (response.status_code, staff_counts(receipt_case)) == (409, before)


def test_replayed_staff_form_cannot_duplicate_a_payment(
    receipt_case: PortalSetup,
) -> None:
    assert staff_post(receipt_case).status_code == 302
    after_first = staff_counts(receipt_case)

    response = staff_post(receipt_case)

    assert (response.status_code, staff_counts(receipt_case)) == (409, after_first)


@pytest.mark.parametrize("kind", ["payment", "guide_issued"])
def test_staff_fact_commits_one_event_and_one_cycle_revision(
    receipt_case: PortalSetup, kind: str
) -> None:
    before = staff_counts(receipt_case)

    response = staff_post(receipt_case, kind=kind)

    assert response.status_code == 302
    assert staff_counts(receipt_case) == tuple(value + 1 for value in before)
    with tenant_context(receipt_case.graph.tenant.pk):
        fact = SourceObservation.objects.get(
            subject_ref_id=receipt_case.request.obligation_id, kind=kind
        )
        event = CycleEvent.objects.filter(cycle=receipt_case.cycle).latest(
            "occurred_at"
        )
        assert event.payload["observation_id"] == str(fact.pk)


@pytest.mark.parametrize("foreign_identity", ["client", "period"])
def test_staff_subject_is_bound_to_the_rendered_cycle(
    receipt_case: PortalSetup, foreign_identity: str
) -> None:
    with tenant_context(receipt_case.graph.tenant.pk):
        if foreign_identity == "client":
            generate_das_calendar(receipt_case.other_client, starting_from=PERIOD)
            foreign = Obligation.objects.get(
                client=receipt_case.other_client, competence_month=PERIOD
            )
        else:
            foreign = Obligation.objects.get(
                client=receipt_case.graph.client, competence_month=date(2026, 10, 1)
            )
    before = staff_counts(receipt_case)

    response = staff_post(receipt_case, subject_id=str(foreign.pk))

    assert (response.status_code, staff_counts(receipt_case)) == (404, before)


@pytest.mark.parametrize("operation", ["correct", "retract"])
def test_headless_staff_correction_is_not_a_new_root(
    receipt_case: PortalSetup, operation: str
) -> None:
    before = staff_counts(receipt_case)

    response = staff_post(receipt_case, op=operation, head_id="")

    assert (response.status_code, staff_counts(receipt_case)) == (422, before)


def test_staff_fact_retains_the_optional_scoped_evidence(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    page = receipt_case.firm().get(f"/ciclos/{receipt_case.cycle.pk}/")
    assert page.status_code == 200
    controls = re.findall(
        r'<select[^>]+name="evidence_link_id"[^>]*>(.*?)</select>',
        page.content.decode(),
        re.DOTALL,
    )
    assert controls
    assert all(f'<option value="{link.pk}"' in control for control in controls)

    response = staff_post(receipt_case, evidence_link_id=str(link.pk))

    assert response.status_code == 302
    with tenant_context(receipt_case.graph.tenant.pk):
        fact = SourceObservation.objects.get(kind="payment", outcome="recorded")
        assert fact.evidence_link_id == link.pk
        assert fact.source_kind == "staff_recorded"
        assert fact.verification_status == "unknown"


def test_staff_form_correction_cannot_fork_a_superseded_head(
    receipt_case: PortalSetup,
) -> None:
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        head = record_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
        )
        record_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            expected_head_id=head.pk,
        )
    before = staff_counts(receipt_case)

    response = staff_post(receipt_case, op="correct", head_id=str(head.pk))

    assert (response.status_code, staff_counts(receipt_case)) == (409, before)


def test_a_stale_correction_is_refused_in_plain_sentences(
    receipt_case: PortalSetup,
) -> None:
    """AXR1-054: StaleObservationError is a ValidationError; its 409 reads plainly."""
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        head = record_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
        )
        record_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            expected_head_id=head.pk,
        )

    response = staff_post(receipt_case, op="correct", head_id=str(head.pk))

    assert_readable_page(
        response,
        status=HTTPStatus.CONFLICT,
        reason="O registro observado mudou. Recarregue antes de corrigir.",
    )


def test_cross_client_optional_evidence_is_refused_without_writes(
    receipt_case: PortalSetup,
) -> None:
    outsider = add_member(
        receipt_case.graph.tenant,
        "other-client@t17-repair.test",
        TenantRole.CLIENT_OWNER,
        client=receipt_case.other_client,
    )
    foreign = replace(
        receipt_case,
        request=receipt_case.other_request,
        portal_user=outsider,
    )
    intake = submit(foreign, "recibo_cliente.pdf")
    with tenant_context(receipt_case.graph.tenant.pk):
        link = EvidenceLink.objects.get(document_id=intake.document_id)
    page = receipt_case.firm().get(f"/ciclos/{receipt_case.cycle.pk}/")
    assert page.status_code == 200
    controls = re.findall(
        r'<select[^>]+name="evidence_link_id"[^>]*>(.*?)</select>',
        page.content.decode(),
        re.DOTALL,
    )
    assert controls
    assert all(f'<option value="{link.pk}"' not in control for control in controls)
    before = staff_counts(receipt_case)

    response = staff_post(receipt_case, evidence_link_id=str(link.pk))

    assert (response.status_code, staff_counts(receipt_case)) == (404, before)


def test_revoked_staff_post_has_no_observation_event_or_version_effect(
    receipt_case: PortalSetup,
) -> None:
    graph = receipt_case.graph
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
    browser = receipt_case.firm(graph.staff)
    with tenant_context(graph.tenant.pk):
        Membership.objects.filter(tenant=graph.tenant, user=graph.staff).update(
            is_active=False
        )
    before = staff_counts(receipt_case)

    response = browser.post(
        f"/ciclos/{receipt_case.cycle.pk}/",
        {
            "action": "observation",
            "op": "record",
            "kind": "payment",
            "subject_id": str(receipt_case.request.obligation_id),
            "expected_version": str(receipt_case.cycle.version),
        },
    )

    assert response.status_code in {403, 404}
    assert staff_counts(receipt_case) == before


@pytest.mark.parametrize("bad_input", ["kind", "period"])
def test_staff_service_rejects_a_mismatched_fact_identity(
    receipt_case: PortalSetup, bad_input: str
) -> None:
    subject = payment_subject(receipt_case)
    before = staff_counts(receipt_case)

    with (
        tenant_context(subject.tenant_id),
        pytest.raises((ValidationError, Http404)),
    ):
        record_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="calendar_scheduled" if bad_input == "kind" else "payment",
            period=date(2026, 10, 1) if bad_input == "period" else PERIOD,
        )

    assert staff_counts(receipt_case) == before


def test_current_nullable_period_claim_can_be_corrected_without_changing_identity(
    receipt_case: PortalSetup,
) -> None:
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        previous = SourceObservation.objects.create(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_ref=subject,
            kind="payment",
            period=None,
            source_kind="legacy_status",
            verification_status="unknown",
            observed_at=CLOCK,
        )

    response = staff_post(receipt_case, op="correct", head_id=str(previous.pk))

    assert response.status_code == 302
    with tenant_context(subject.tenant_id):
        correction = SourceObservation.objects.get(supersedes=previous)
        assert correction.period is None
        assert coverage_item(receipt_case).head_id == correction.pk
