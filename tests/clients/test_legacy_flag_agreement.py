"""The guard that keeps `is_mei` and the tax timeline from drifting apart.

While the legacy flag still exists, every reader that matters consults the
effective-dated profile — but rows also still carry `is_mei`, and a divergence is
not a disagreement about style: it means the DAS sweep and the threshold monitor
answer a different question than the registry shows. This module is the tripwire.
It iterates EVERY client of EVERY tenant and asserts the flag and today's claim
agree — `is_mei` is true exactly when the profile covering today says `mei`.

Two deliberate sharpenings over the bare comparison:

* A client `is_mei=True` with **no current claim at all** disagrees. `None.regime`
  cannot equal "mei", and pretending a missing timeline is "not-MEI" would let the
  dual-write fail open on exactly the clients it forgot.
* The check reads `profile_at`, the same function readers use, rather than
  recomputing the range predicate — so a bug in the reader and a bug in the guard
  cannot silently agree with each other.

The negative cases live in the same file: a client whose claim stopped yesterday
(a future regime is not today's), and a deliberately flipped profile, which is the
failure shape this guard exists to catch — the flag stayed `True` while the
timeline learned better.
"""

from datetime import date, timedelta
from uuid import UUID

import pytest
from django.utils import timezone

from apps.clients.models import ClientCompany, TaxProfile, TaxRegime
from apps.clients.services import profile_at
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)


def disagreements() -> list[UUID]:
    """Return the ids of every client whose flag and today's claim disagree."""
    today = timezone.localdate()
    offenders: list[UUID] = []
    for tenant in Tenant.objects.all().order_by("slug"):
        with tenant_context(tenant.id):
            for client in ClientCompany.objects.all().order_by("pk"):
                profile = profile_at(client, today)
                is_mei = profile is not None and profile.regime == TaxRegime.MEI
                if client.is_mei != is_mei:
                    offenders.append(client.pk)
    return offenders


def _client(
    tenant: Tenant,
    *,
    legal_name: str,
    cnpj: str,
    is_mei: bool,
) -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name=legal_name,
            cnpj=cnpj,
            is_mei=is_mei,
        )


def _claim(
    client: ClientCompany,
    *,
    regime: str,
    valid_from: date,
    valid_to: date | None = None,
    superseded: bool = False,
) -> None:
    with tenant_context(client.tenant_id):
        row = TaxProfile(
            tenant_id=client.tenant_id,
            client=client,
            regime=regime,
            valid_from=valid_from,
            valid_to=valid_to,
            provenance="test",
        )
        if superseded:
            row.superseded_at = timezone.now()
        row.save()


def test_every_client_flag_agrees_with_the_claim_covering_today() -> None:
    """The sweep's contract: flag and timeline tell the same story, everywhere."""
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    alpha = Tenant.objects.create(name="Firma Alpha", slug="agree-alpha")
    beta = Tenant.objects.create(name="Firma Beta", slug="agree-beta")

    mei = _client(
        alpha, legal_name="MEI Verdadeiro", cnpj="12ABC34501DE35", is_mei=True
    )
    _claim(mei, regime=TaxRegime.MEI, valid_from=date(2020, 1, 1))

    not_mei = _client(alpha, legal_name="Nao MEI", cnpj="33XYZ98765QR00", is_mei=False)
    _claim(not_mei, regime=TaxRegime.SIMPLES, valid_from=date(2020, 1, 1))

    unknown = _client(
        alpha, legal_name="Regime Incerto", cnpj="99QQQ11122XX00", is_mei=False
    )
    _claim(unknown, regime=TaxRegime.UNKNOWN, valid_from=date(2020, 1, 1))

    # is_mei=False with NO claim is agreement, not an offence: nothing asserts MEI
    # on either side, and the honest unknown must not read as a defect.
    _client(alpha, legal_name="Sem Perfil", cnpj="55WWWW55555WW5", is_mei=False)

    # A claim that closed yesterday does not count for today, and a superseded row
    # is invisible to the check — the flag carries the truth while the timeline is
    # corrected.
    ended = _client(beta, legal_name="Ex MEI", cnpj="77EEEE77777EE7", is_mei=False)
    _claim(
        ended,
        regime=TaxRegime.MEI,
        valid_from=date(2020, 1, 1),
        valid_to=yesterday,
    )
    corrected = _client(
        beta, legal_name="Corrigido", cnpj="44DDDD44444DD4", is_mei=False
    )
    _claim(
        corrected,
        regime=TaxRegime.MEI,
        valid_from=date(2020, 1, 1),
        superseded=True,
    )
    _claim(corrected, regime=TaxRegime.SIMPLES, valid_from=date(2020, 1, 1))

    assert disagreements() == []


def test_a_flag_true_client_with_no_claim_is_a_disagreement() -> None:
    """Silence is not 'not-MEI' when the flag says MEI."""
    tenant = Tenant.objects.create(name="Firma Muda", slug="agree-mudo")
    silent = _client(tenant, legal_name="Sem Claim", cnpj="12ABC34501DE35", is_mei=True)

    assert disagreements() == [silent.pk]


def test_a_profile_flip_without_the_flag_is_a_disagreement() -> None:
    """The failure this guard exists to name: the timeline moved, the flag did not."""
    today = timezone.localdate()
    tenant = Tenant.objects.create(name="Firma Divergente", slug="agree-div")
    flipped = _client(
        tenant, legal_name="Ex MEI Divergente", cnpj="12ABC34501DE35", is_mei=True
    )
    # The claim covering today says Simples — exactly the drift a retroactive
    # desenquadramento produces when only one of the two writers ran.
    _claim(flipped, regime=TaxRegime.SIMPLES, valid_from=today)

    assert disagreements() == [flipped.pk]
