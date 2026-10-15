"""The portal says where a "paid" claim came from — and never more than it knows.

S6.4 separates work status from payment evidence: `Obligation.status == 'paid'`
says the firm recorded a payment, and the `SourceObservation` backfill labels that
fact `source_kind='legacy_status'` with `verification_status='unknown'`. On the
"Pagamentos e guias" page, a flagged (`cycles_r1`) tenant's client must read
"Pagamento registrado manualmente (legado)" — S5.7's vocabulary for an informed,
unverified payment — and must never read "confirmado", which is a word this page
cannot honestly say about a legacy flag.

The flag gate is the rollout contract: an UNFLAGGED tenant's page renders exactly
what it rendered before, legacy claim or not — the observation read does not even
run — so cohort-by-cohort the label is a feature, not a flag day.

The assertions stay at arm's length from the HTML: the sentence, its absence, and
the forbidden word. How the badge is styled is presentation's business; what it
SAYS is the contract.
"""

import datetime as dt
from http import HTTPStatus
from typing import Final

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    Obligation,
    ObligationStatus,
    ObligationType,
    SourceObservation,
)
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

# The portal host embeds the firm's slug: acme-portal.localhost routes to acme.
# A request to a host naming no tenant resolves no client context at all, and the
# page renders its empty state — a 200 that would pass absence assertions anyway,
# so the host is built from the tenant rather than spelled once.
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

LEGACY_LABEL: Final = "Pagamento registrado manualmente (legado)"
LEGACY_COPY: Final = (
    "Pagamento registrado manualmente (legado). Ainda não verificamos junto à Receita."
)
OLD_PAID_COPY: Final = "Nada a fazer. O pagamento já foi registrado pela sua contadora."
# Substring case-folded: the page must not SAY the word anywhere, whatever the
# inflection — S5.7 reserves it for positively verified states.
FORBIDDEN_WORD: Final = "confirmado"

COMPETENCE: Final = dt.date(2026, 9, 1)


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


def _firm(slug: str, *, features: list[str]) -> tuple[Tenant, ClientCompany, User]:
    """One firm, one client with a legacy-paid obligation, one signed-in owner."""
    tenant = Tenant.objects.create(name=slug.title(), slug=slug, features=features)
    with tenant_context(tenant.id):
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name=f"CLIENTE {slug.upper()}",
            cnpj="11222333000181",
            is_mei=True,
        )
        obligation = Obligation.objects.create(
            tenant=tenant,
            client=client,
            obligation_type=ObligationType.objects.get(pk="DAS"),
            competence_month=COMPETENCE,
            nominal_due_date=dt.date(2026, 10, 20),
            resolved_due_date=dt.date(2026, 10, 20),
            status=ObligationStatus.PAID,
        )
        # The row the backfill writes: the flag said paid, nothing verified it.
        SourceObservation.objects.create(
            tenant=tenant,
            client=client,
            subject_ref=obligation,
            kind="payment",
            period=COMPETENCE,
            source_kind="legacy_status",
            observed_at=timezone.now(),
            verification_status="unknown",
        )
    owner = User.objects.create_user(
        email=f"dono@{slug}.example",
        password=PASSWORD,
    )
    enrol_totp(owner)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=client,
    )
    return tenant, client, owner


def _payments(tenant: Tenant, owner: User) -> str:
    """GET the payments page as `owner` on `tenant`'s own portal host."""
    http = Client()
    http.force_login(owner)
    response = http.get(
        str(reverse("portal-payments", urlconf=PORTAL_URLCONF)),
        headers={"host": f"{tenant.slug}-portal.localhost"},
    )
    assert response.status_code == HTTPStatus.OK, (
        f"the payments page answered {response.status_code}; nothing below is a "
        f"rendered document"
    )
    return str(response.content.decode())


def test_a_flagged_tenant_labels_the_legacy_paid_row_as_manually_recorded() -> None:
    # Given a cycles_r1 tenant whose client's paid obligation is legacy-observed
    tenant, _client, owner = _firm("flagged-obs", features=["cycles_r1"])

    # When the payments page renders
    body = _payments(tenant, owner)

    # Then the claim is labelled by its source, and the legacy rendering is gone
    assert LEGACY_LABEL in body
    assert LEGACY_COPY in body
    assert OLD_PAID_COPY not in body

    # And the page never claims what nobody verified — "confirmado" in any casing
    assert FORBIDDEN_WORD not in body.casefold()


def test_an_unflagged_tenant_renders_the_paid_row_exactly_as_before() -> None:
    # Given the identical data on a tenant WITHOUT cycles_r1
    tenant, _client, owner = _firm("unflagged-obs", features=[])

    # When the payments page renders
    body = _payments(tenant, owner)

    # Then nothing about the flag row changed: the old copy stands and the new
    # label never appeared, because the observation read was skipped outright
    assert OLD_PAID_COPY in body
    assert LEGACY_LABEL not in body
    assert FORBIDDEN_WORD not in body.casefold()


def test_a_flagged_tenant_with_a_nonlegacy_manual_source_stays_unverified() -> None:
    # Given a flagged tenant whose paid row is observed by a NON-legacy source:
    # staff recorded it, which is informed-but-not-legacy
    tenant, client, owner = _firm("staff-obs", features=["cycles_r1"])
    with tenant_context(tenant.id):
        obligation = Obligation.objects.get(client=client)
        SourceObservation.objects.create(
            tenant=tenant,
            client=client,
            subject_ref=obligation,
            kind="payment",
            period=COMPETENCE,
            source_kind="staff_recorded",
            # Newer than the legacy row: the latest assertion is what labels.
            observed_at=timezone.now() + dt.timedelta(hours=1),
            verification_status="pending_review",
        )

    # When the payments page renders
    body = _payments(tenant, owner)

    # Then the legacy wording is absent: the label belongs to legacy_status alone,
    # and a staff-recorded payment does not borrow its parenthesis or paid certainty.
    assert LEGACY_LABEL not in body
    assert OLD_PAID_COPY not in body
    assert 'data-payment-state="manual"' in body
    assert 'class="badge badge-ok"' not in body
    assert FORBIDDEN_WORD not in body.casefold()
