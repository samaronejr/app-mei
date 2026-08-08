from typing import Final

import pytest
from django.template import Context, Template

from apps.clients.models import ClientStatus, OnboardingStatus
from apps.core.templatetags import ptbr
from apps.obligations.models import ObligationStatus
from apps.tenants.models import TenantRole

FILTER_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    ("papel", TenantRole.OWNER.value, "Proprietário"),
    ("papel", TenantRole.STAFF_ACCOUNTANT.value, "Contador"),
    (
        "papel",
        TenantRole.OPERATIONS_ADMIN.value,
        "Administrador de operações",
    ),
    ("papel", TenantRole.CLIENT_OWNER.value, "Titular do MEI"),
    (
        "papel",
        TenantRole.CLIENT_COLLABORATOR.value,
        "Colaborador do MEI",
    ),
    ("situacao_cliente", ClientStatus.ONBOARDING.value, "Em onboarding"),
    ("situacao_cliente", ClientStatus.ACTIVE.value, "Ativo"),
    ("situacao_cliente", ClientStatus.SUSPENDED.value, "Suspenso"),
    ("situacao_cliente", ClientStatus.CLOSED.value, "Encerrado"),
    (
        "situacao_obrigacao",
        ObligationStatus.SCHEDULED.value,
        "Programada",
    ),
    ("situacao_obrigacao", ObligationStatus.DUE.value, "A pagar"),
    ("situacao_obrigacao", ObligationStatus.PAID.value, "Paga"),
    ("situacao_obrigacao", ObligationStatus.OVERDUE.value, "Em atraso"),
    ("situacao_obrigacao", ObligationStatus.WAIVED.value, "Dispensada"),
    ("situacao_onboarding", OnboardingStatus.PENDING.value, "Pendente"),
    ("situacao_onboarding", OnboardingStatus.BLOCKED.value, "Travado"),
    ("situacao_onboarding", OnboardingStatus.DONE.value, "Concluído"),
    (
        "situacao_onboarding",
        OnboardingStatus.NOT_APPLICABLE.value,
        "Não se aplica",
    ),
)


def test_papel_map_covers_every_tenant_role() -> None:
    labels = getattr(ptbr, "TENANT_ROLE_LABELS", {})
    assert "papel" in ptbr.register.filters, "the papel filter does not exist"
    missing = [
        f"TenantRole.{member.name}"
        for member in TenantRole
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in TenantRole})
    assert set(labels) == {member.value for member in TenantRole}, (
        f"TENANT_ROLE_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_situacao_cliente_map_covers_every_client_status() -> None:
    labels = getattr(ptbr, "CLIENT_STATUS_LABELS", {})
    assert "situacao_cliente" in ptbr.register.filters, (
        "the situacao_cliente filter does not exist"
    )
    missing = [
        f"ClientStatus.{member.name}"
        for member in ClientStatus
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in ClientStatus})
    assert set(labels) == {member.value for member in ClientStatus}, (
        f"CLIENT_STATUS_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_situacao_obrigacao_map_covers_every_obligation_status() -> None:
    labels = getattr(ptbr, "OBLIGATION_STATUS_LABELS", {})
    assert "situacao_obrigacao" in ptbr.register.filters, (
        "the situacao_obrigacao filter does not exist"
    )
    missing = [
        f"ObligationStatus.{member.name}"
        for member in ObligationStatus
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in ObligationStatus})
    assert set(labels) == {member.value for member in ObligationStatus}, (
        f"OBLIGATION_STATUS_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_situacao_onboarding_map_covers_every_onboarding_status() -> None:
    labels = getattr(ptbr, "ONBOARDING_STATUS_LABELS", {})
    assert "situacao_onboarding" in ptbr.register.filters, (
        "the situacao_onboarding filter does not exist"
    )
    missing = [
        f"OnboardingStatus.{member.name}"
        for member in OnboardingStatus
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in OnboardingStatus})
    assert set(labels) == {member.value for member in OnboardingStatus}, (
        f"ONBOARDING_STATUS_LABELS is missing {missing} and has unexpected {unexpected}"
    )


@pytest.mark.parametrize(("filter_name", "value", "expected"), FILTER_CASES)
def test_each_filter_renders_the_approved_word(
    filter_name: str,
    value: str,
    expected: str,
) -> None:
    assert ptbr.register.filters[filter_name](value) == expected


@pytest.mark.parametrize(
    "filter_name",
    [
        "papel",
        "situacao_cliente",
        "situacao_obrigacao",
        "situacao_onboarding",
    ],
)
def test_unmapped_values_render_desconhecido_without_disclosing_the_raw_value(
    filter_name: str,
) -> None:
    raw_value = "internal_future_value"
    rendered = ptbr.register.filters[filter_name](raw_value)

    assert rendered == "Desconhecido"
    assert raw_value not in rendered


def test_the_filters_are_available_to_templates() -> None:
    planted = (
        "{% load ptbr %}"
        "{{ role|papel }}|"
        "{{ client|situacao_cliente }}|"
        "{{ obligation|situacao_obrigacao }}|"
        "{{ onboarding|situacao_onboarding }}"
    )
    rendered = Template(planted).render(
        Context(
            {
                "role": TenantRole.OWNER.value,
                "client": ClientStatus.ACTIVE.value,
                "obligation": ObligationStatus.DUE.value,
                "onboarding": OnboardingStatus.BLOCKED.value,
            }
        )
    )

    assert rendered == "Proprietário|Ativo|A pagar|Travado"
