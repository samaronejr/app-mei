"""Real HTTP conflicts, malformed inputs and portfolio denials never write."""

from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from uuid import UUID

import pytest
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.cycles.workspace import timeline
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.support import enrol_totp
from tests.ui.refusal_contract import assert_readable_page, assert_readable_refusal

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True)
class Refusal:
    """One refused cycle request: what it sends, says and leads back to."""

    path: str
    data: dict[str, str] | None
    reason: str
    back: str


def _signed_in(user: User, host: str) -> Client:
    enrol_totp(user)
    session = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(user, f"http://{host}/")
    session.cookies[cookie["name"]] = cookie["value"]
    return session


def _opened(slug: str) -> tuple[UUID, Client]:
    setup = cycle_setup(slug)
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key=slug,
        )
    return ref.id, _signed_in(setup.actor, f"{setup.tenant.slug}.localhost")


def _cycle_refusals(setup: CycleSetup) -> dict[str, Refusal]:
    cycles = reverse("cycle-list")
    workspace = reverse("client-detail", args=[setup.client.pk])
    opening = reverse("cycle-open", args=[setup.client.pk])
    # The F3 path: a real engagement and a published playbook, at a competence the
    # engagement does not cover yet, so `open_cycle` raises its ValidationError.
    ineligible = {
        "engagement_id": str(setup.engagement.pk),
        "playbook_version_id": str(setup.definition.pk),
        "periodo": "2026-08",
    }
    return {
        "filtro-competencia": Refusal(
            f"{cycles}?periodo=2026-13", None, "Filtros inválidos.", cycles
        ),
        "filtro-responsavel": Refusal(
            f"{cycles}?responsavel=bad", None, "Filtros inválidos.", cycles
        ),
        "espaco-competencia": Refusal(
            f"{workspace}?periodo=bad",
            None,
            "Competência inválida. Use AAAA-MM.",
            workspace,
        ),
        "abrir-formulario": Refusal(
            opening, {}, "Dados inválidos para abrir o ciclo.", workspace
        ),
        "abrir-inelegivel": Refusal(
            opening,
            ineligible,
            "O serviço e o perfil fiscal não correspondem "
            "a esta competência e playbook.",
            f"{workspace}?periodo=2026-08",
        ),
    }


@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
def test_stale_post_409_and_unassigned_404_zero_writes() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="http",
        )
    host = f"{setup.tenant.slug}.localhost"
    owner, staff = Client(SERVER_NAME=host), Client(SERVER_NAME=host)
    for user, session in [(setup.actor, owner), (setup.staff, staff)]:
        enrol_totp(user)
        cookie = mfa_session_cookie(user, f"http://{host}/")
        session.cookies[cookie["name"]] = cookie["value"]
    url = f"/ciclos/{ref.id}/"
    assert owner.get(url).status_code == 200
    data = {"action": "transition", "to_state": "em_andamento", "expected_version": 1}
    assert owner.post(url, data).status_code == 302
    assert owner.post(url, {**data, "to_state": "cancelado"}).status_code == 409
    for path in [url, f"/clientes/{setup.client.pk}/?periodo=2026-09"]:
        assert staff.get(path).status_code == 404
    assert staff.post(url, {**data, "expected_version": 2}).status_code == 404
    denied = staff.get("/ciclos/?periodo=2026-09")
    assert denied.status_code == 200
    assert denied.context["page"].paginator.count == 0
    for path in [
        "/ciclos/?periodo=2026-13",
        "/ciclos/?responsavel=bad",
        f"/clientes/{setup.client.pk}/?periodo=bad",
    ]:
        assert owner.get(path).status_code == 422
    assert (
        owner.post(url, {**data, "expected_version": "not-an-integer"}).status_code
        == 422
    )
    with tenant_context(setup.tenant.pk):
        assert CycleEvent.objects.count() == 2
        assert ServiceCycle.objects.get().state == "em_andamento"
    setup.tenant.features = []
    setup.tenant.save(update_fields=["features"])
    assert owner.get(url).status_code == 404
    assert owner.get("/ciclos/").status_code == 404


@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
@pytest.mark.parametrize(
    "case",
    [
        "filtro-competencia",
        "filtro-responsavel",
        "espaco-competencia",
        "abrir-formulario",
        "abrir-inelegivel",
    ],
)
def test_a_cycle_refusal_is_a_readable_page_with_a_way_back(case: str) -> None:
    """AXR1-054: a refused cycle request keeps its 422 and reads as a page."""
    setup = cycle_setup(f"refusal-{case}")
    owner = _signed_in(setup.actor, f"{setup.tenant.slug}.localhost")
    refusal = _cycle_refusals(setup)[case]
    response = (
        owner.get(refusal.path)
        if refusal.data is None
        else owner.post(refusal.path, refusal.data)
    )
    assert_readable_refusal(
        response,
        status=HTTPStatus.UNPROCESSABLE_CONTENT,
        reason=refusal.reason,
        back=refusal.back,
    )
    with tenant_context(setup.tenant.pk):
        assert not ServiceCycle.objects.exists()


@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
def test_a_cycle_page_error_reads_in_plain_sentences() -> None:
    """AXR1-054: a ValidationError behind the cycle form is shown as its sentence."""
    cycle_id, owner = _opened("refusal-observacao")
    response = owner.post(
        reverse("cycle-detail", args=[cycle_id]),
        {"action": "observation", "expected_version": "1"},
    )
    assert_readable_page(
        response,
        status=HTTPStatus.UNPROCESSABLE_CONTENT,
        reason="Informe a obrigação, o fato e a ação.",
    )


@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
@pytest.mark.parametrize(
    ("data", "status", "reason"),
    [
        ({}, HTTPStatus.UNPROCESSABLE_CONTENT, "Revise os dados informados."),
        (
            {"expected_version": "1", "reason": "Corrigir a guia."},
            HTTPStatus.UNPROCESSABLE_CONTENT,
            "Informe a conclusão, o motivo e o escopo da emenda.",
        ),
        (
            {"expected_version": "99", "reason": "Corrigir a guia."},
            HTTPStatus.CONFLICT,
            "Este registro mudou. Recarregue antes de salvar.",
        ),
    ],
    ids=["revisao-invalida", "sem-conclusao", "revisao-antiga"],
)
def test_a_refused_reopen_is_a_readable_page_with_a_way_back(
    data: dict[str, str], status: HTTPStatus, reason: str
) -> None:
    """AXR1-054: the reopen form's refusals keep their status and read as a page."""
    cycle_id, owner = _opened("refusal-reabrir")
    response = owner.post(reverse("cycle-reopen", args=[cycle_id]), data)
    assert_readable_refusal(
        response,
        status=status,
        reason=reason,
        back=reverse("cycle-detail", args=[cycle_id]),
    )


def test_timeline_merges_references_only_with_client_and_period_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2026, 9, 15, tzinfo=UTC)
    )
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="timeline",
        )
        own = record_event(
            action=AuditAction.CLIENT_UPDATED,
            tenant_id=setup.tenant.pk,
            actor=setup.actor,
            obj=ObjectRef(type="clients.ClientCompany", id=str(setup.client.pk)),
            metadata={
                "client_id": str(setup.client.pk),
                "untrusted_text": "not a timeline field",
            },
        )
        other = record_event(
            action=AuditAction.CLIENT_UPDATED,
            tenant_id=setup.tenant.pk,
            actor=setup.actor,
            obj=ObjectRef(type="clients.ClientCompany", id=str(setup.engagement.pk)),
            metadata={"client_id": str(setup.engagement.pk)},
        )
        entries = timeline(setup.actor, setup.client.pk, PERIOD)
        assert {row.id for row in entries} == {
            own.pk,
            CycleEvent.objects.get(cycle_id=ref.id).pk,
        }
        assert other.pk not in {row.id for row in entries}
        assert "untrusted_text" not in str(entries)
        assert timeline(setup.actor, setup.client.pk, PERIOD.replace(month=8)) == []
