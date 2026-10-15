"""Payment provenance, never the work column, controls pilot payment badges."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.template import engines
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    Obligation,
    ObligationStatus,
    ObservationSourceKind,
    ObservationVerificationStatus,
    SourceObservation,
)
from tests.obligations import test_payment_observation_labels as label_tests
from tests.obligations.test_payment_observation_labels import (
    COMPETENCE,
    _firm,
    _payments,
)

pytestmark = pytest.mark.django_db(transaction=True)
_portal_urls = label_tests._portal_urls


@pytest.mark.parametrize(
    "source",
    [
        ObservationSourceKind.LEGACY_STATUS,
        ObservationSourceKind.STAFF_RECORDED,
        ObservationSourceKind.CLIENT_SUBMITTED,
    ],
)
@pytest.mark.parametrize(
    "verification",
    [
        ObservationVerificationStatus.UNKNOWN,
        ObservationVerificationStatus.PENDING_REVIEW,
        ObservationVerificationStatus.VERIFIED,
    ],
)
def test_manual_sources_never_gain_verified_payment_badges(
    source: str, verification: str
) -> None:
    tenant, client, owner = _firm("manual-state", features=["cycles_r1"])
    with tenant_context(tenant.id):
        obligation = Obligation.objects.get(client=client)
        SourceObservation.objects.create(
            tenant=tenant,
            client=client,
            subject_ref=obligation,
            kind="payment",
            period=COMPETENCE,
            source_kind=source,
            verification_status=verification,
            observed_at=timezone.now() + timedelta(hours=1),
        )
    body = _payments(tenant, owner)
    assert 'data-payment-state="manual"' in body
    assert 'class="badge badge-neutral"' in body
    assert 'class="badge badge-ok"' not in body
    badges = re.findall(r'<span class="badge [^"]+">([^<]+)</span>', body)
    assert badges
    assert not any(
        re.search(r"\b(?:pag[ao]|confirmad[ao])\b", badge, re.IGNORECASE)
        for badge in badges
    )


def test_work_status_cannot_hide_or_change_a_payment_observation() -> None:
    tenant, client, owner = _firm("work-independent", features=["cycles_r1"])
    rendered = []
    for status in ObligationStatus.values:
        with tenant_context(tenant.id):
            Obligation.objects.filter(client=client).update(status=status)
        body = _payments(tenant, owner)
        card = re.search(r'<li class="card[^>]*>.*?</li>', body, re.DOTALL)
        assert card is not None
        rendered.append(card.group())
    assert len(set(rendered)) == 1


@pytest.mark.parametrize(
    ("source", "verification", "state"),
    [
        (
            ObservationSourceKind.OFFICIAL_API,
            ObservationVerificationStatus.VERIFIED,
            "verified",
        ),
        (
            ObservationSourceKind.OFFICIAL_FILE_IMPORT,
            ObservationVerificationStatus.VERIFIED,
            "verified",
        ),
        (
            ObservationSourceKind.OFFICIAL_API,
            ObservationVerificationStatus.UNKNOWN,
            "unknown",
        ),
        (
            ObservationSourceKind.OFFICIAL_API,
            ObservationVerificationStatus.STALE,
            "stale",
        ),
        (
            ObservationSourceKind.OFFICIAL_API,
            ObservationVerificationStatus.NOT_APPLICABLE,
            "not_applicable",
        ),
        (
            ObservationSourceKind.AI_INFERENCE,
            ObservationVerificationStatus.VERIFIED,
            "unknown",
        ),
        (
            ObservationSourceKind.DETERMINISTIC_RULE,
            ObservationVerificationStatus.VERIFIED,
            "unknown",
        ),
        ("unrecognized", ObservationVerificationStatus.VERIFIED, "unknown"),
    ],
)
def test_only_verified_official_sources_get_positive_badges(
    source: str, verification: str, state: str
) -> None:
    tenant, client, owner = _firm("official-state", features=["cycles_r1"])
    with tenant_context(tenant.id):
        SourceObservation.objects.create(
            tenant=tenant,
            client=client,
            subject_ref=Obligation.objects.get(client=client),
            kind="payment",
            source_kind=source,
            verification_status=verification,
            observed_at=timezone.now() + timedelta(hours=1),
        )
    body = _payments(tenant, owner)
    assert f'data-payment-state="{state}"' in body
    assert ('class="badge badge-ok"' in body) == (state == "verified")


def test_paid_work_without_payment_evidence_is_unknown() -> None:
    tenant, client, owner = _firm("no-payment", features=["cycles_r1"])
    with tenant_context(tenant.id):
        Obligation.objects.create(
            tenant=tenant,
            client=client,
            obligation_type_id="DAS",
            competence_month=COMPETENCE.replace(month=8),
            nominal_due_date=COMPETENCE.replace(day=20),
            resolved_due_date=COMPETENCE.replace(day=20),
            status=ObligationStatus.PAID,
        )
    body = _payments(tenant, owner)
    assert 'data-payment-state="unknown"' in body
    assert 'class="badge badge-ok"' not in body


@pytest.mark.parametrize("kind", ["filing", "guide_issued"])
def test_other_observation_kinds_cannot_verify_payment(kind: str) -> None:
    tenant, client, owner = _firm("not-payment", features=["cycles_r1"])
    with tenant_context(tenant.id):
        SourceObservation.objects.create(
            tenant=tenant,
            client=client,
            subject_ref=Obligation.objects.get(client=client),
            kind=kind,
            source_kind=ObservationSourceKind.OFFICIAL_API,
            verification_status=ObservationVerificationStatus.VERIFIED,
            observed_at=timezone.now() + timedelta(hours=1),
        )
    body = _payments(tenant, owner)
    assert 'data-payment-state="manual"' in body
    assert 'class="badge badge-ok"' not in body


def test_unflagged_payment_render_is_byte_identical_without_observation_queries() -> (
    None
):
    tenant, _client, owner = _firm("unflagged-state", features=[])
    http = Client()
    http.force_login(owner)
    with CaptureQueriesContext(connection) as queries:
        response = http.get(
            "/pagamentos/", headers={"host": f"{tenant.slug}-portal.localhost"}
        )
    assert response.status_code == 200
    assert not any("obligations_sourceobservation" in q["sql"] for q in queries)
    context = {
        "payments": response.context["payments"],
        "page": response.context["page"],
        "cycles_r1": response.context["cycles_r1"],
        "csrf_token": str(response.context["csrf_token"]),
    }
    request = response.wsgi_request
    # Same request/context; the fixture is the template shipped at base 6d92ef4.
    legacy = Path("tests/portal/fixtures/payments_pre_observations.html").read_text()
    with override_settings(ROOT_URLCONF="apps.portal.urls"):
        before = engines["django"].from_string(legacy).render(context, request=request)
        after = render_to_string("portal/payments.html", context, request=request)
    assert before.encode() == after.encode()
    assert "data-payment-state" not in after
