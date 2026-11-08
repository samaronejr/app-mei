"""The Hoje projection of an owned federal-check problem, field by field."""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.briefing import providers
from apps.briefing.rules import BRIEFING_RULES_V1, RULE_LABELS
from apps.briefing.source_rows import SourceContext, SourceRow
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.cycles.services.exceptions import reason_for
from apps.integrations.models import IntegrationIssue
from tests.briefing.fixtures import NOW, briefing_setup
from tests.cycles.cycle_fixtures import CycleSetup

pytestmark = pytest.mark.django_db(transaction=True)

REASON = "autorizacao_ausente"


def _checked[T](call: Callable[[], T]) -> T:
    """Turn any crash of the projection into a failed assertion about it."""
    try:
        return call()
    except Exception as error:
        message = f"the federal projection must not raise: {error!r}"
        raise AssertionError(message) from error


def _issue(
    setup: CycleSetup,
    client: ClientCompany,
    **fields: Any,  # noqa: ANN401
) -> IntegrationIssue:
    return IntegrationIssue.objects.create(
        tenant=setup.tenant,
        client=client,
        reason=REASON,
        service="pagtoweb",
        owner=setup.actor,
        **fields,
    )


def test_an_owned_problem_projects_every_field_of_its_hoje_item() -> None:
    setup = briefing_setup()
    review = NOW.date()
    due = review + timedelta(days=3)
    with tenant_context(setup.tenant.pk):
        ServiceCycle.objects.update(state=CycleState.COMPLETED)
        other = ClientCompany.objects.create(
            tenant=setup.tenant, legal_name="Segunda empresa", cnpj="12345678000195"
        )
        first = _issue(setup, setup.client, review_on=review)
        second = _issue(setup, other, review_on=review, due_on=due)
        context = SourceContext(clients=ClientCompany.objects.all(), now=timezone.now())
        items = {
            item.subject_ref: item
            for item in _checked(lambda: providers.source_items(context))
            if item.kind == "integration"
        }
    assert set(items) == {first.pk, second.pk}
    for issue, client, expected_due in (
        (first, setup.client, review),
        (second, other, due),
    ):
        item = items[issue.pk]
        assert item.client_id == client.pk
        assert item.client_name == client.legal_name
        assert item.owner_id == setup.actor.pk
        assert item.owner_name == setup.actor.email
        assert item.due_on == expected_due
        assert item.period is None
        assert item.work_state == issue.state
        assert item.fact_version == str(issue.version)
        assert item.source_observed_at == issue.created_at
        assert item.service_kind == ""
        assert item.next_action
        assert item.url == reverse("client-detail", args=[client.pk])
        reason = reason_for(REASON)
        assert reason is not None
        assert item.title == reason.label
        assert item.title != REASON
        assert item.factors == (("provider_check_attention", 25),)
        assert item.reasons == ((RULE_LABELS["provider_check_attention"], 25),)


def test_the_attention_rule_has_its_weight_and_a_readable_label() -> None:
    assert BRIEFING_RULES_V1["provider_check_attention"] == 25
    label = RULE_LABELS["provider_check_attention"]
    assert label
    assert label != "provider_check_attention"


def test_a_problem_with_an_unregistered_reason_gets_a_generic_title() -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        ServiceCycle.objects.update(state=CycleState.COMPLETED)
        IntegrationIssue.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            reason="motivo_sem_registro_federal",
            service="pagtoweb",
            owner=setup.actor,
            review_on=date(2026, 9, 20),
        )
        context = SourceContext(clients=ClientCompany.objects.all(), now=timezone.now())
        found = _checked(lambda: providers.source_items(context))
        items = [i for i in found if i.kind == "integration"]
    assert len(items) == 1
    assert items[0].title
    assert items[0].title != "motivo_sem_registro_federal"


def test_the_integration_source_rows_are_firm_only_and_not_client_visible() -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        ServiceCycle.objects.update(state=CycleState.COMPLETED)
        issue = _issue(setup, setup.client, review_on=NOW.date())
        context = SourceContext(clients=ClientCompany.objects.all(), now=timezone.now())
        rows = [
            SourceRow(*row)
            for row in _checked(lambda: providers.PROVIDERS["integration"](context))
        ]
    assert [row.subject_ref for row in rows] == [issue.pk]
    assert rows[0].client_visible is False
