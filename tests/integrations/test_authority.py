"""AXRM-008: immutable authority revisions and fail-closed dispatch admission."""

from datetime import date, timedelta
from importlib import import_module
from types import ModuleType
from typing import Final

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.http import Http404

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.services import (
    IntegrationConflictError,
    InvalidTransitionError,
)
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db
TODAY: Final = date(2026, 11, 10)


def authority_api() -> ModuleType:
    """Assert the feature exists before importing, so the initial red collects."""
    registered = {
        model.__name__ for model in apps.get_app_config("integrations").get_models()
    }
    assert {"AuthorityRecord", "FirmAuthorization", "IntegrationIssue"} <= registered
    return import_module("apps.integrations.authority")


@pytest.fixture
def firm() -> Firm:
    firm = make_firm("authority", client_count=2)
    firm.tenant.features = [
        "cycles_r1",
        "scan_required",
        "federal_checks",
        "document_capture",
    ]
    firm.tenant.save(update_fields=["features"])
    return firm


def test_declaration_verification_and_revocation_preserve_the_chain(firm: Firm) -> None:
    api = authority_api()
    client = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        first = api.declare_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=90),
            expected_version=0,
            today=TODAY,
        )
        assert first.verified_by_id is None
        assert first.status == "ativa"
        assert api.authority_for(client, "SP-PAG", today=TODAY).state == "usable"
        second = api.verify_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            expected_version=1,
            today=TODAY,
        )
        assert second.supersedes_id == first.pk
        assert second.verified_by_id == firm.owner.pk
        revoked = api.revoke_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            expected_version=2,
            today=TODAY,
        )
        assert revoked.supersedes_id == second.pk
        first.refresh_from_db()
        assert first.status == "ativa"
        result = api.authority_for(client, "SP-PAG", today=TODAY)
        assert result.state == "missing"
        assert result.reason == "revogada"
        with pytest.raises(InvalidTransitionError):
            api.revoke_authority(
                firm.owner,
                client.pk,
                service="pagtoweb",
                expected_version=3,
                today=TODAY,
            )
        assert apps.get_model("integrations", "AuthorityRecord").objects.count() == 3


@pytest.mark.parametrize(
    ("offset", "state", "reason"),
    [
        (-1, "unknown", "ainda_nao_vigente"),
        (0, "usable", "vencendo"),
        (30, "usable", "vencendo"),
        (31, "expired", "vencida"),
    ],
)
def test_expiry_uses_the_injected_date(
    firm: Firm,
    offset: int,
    state: str,
    reason: str,
) -> None:
    api = authority_api()
    client = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        api.declare_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=30),
            expected_version=0,
            today=TODAY,
        )
        result = api.authority_for(
            client, "SP-PAG", today=TODAY + timedelta(days=offset)
        )
        assert result.state == state
        assert result.reason == reason


def test_catalog_exemption_needs_no_record_and_unknown_operations_fail_closed(
    firm: Firm,
) -> None:
    api = authority_api()
    with tenant_context(firm.tenant.pk):
        assert (
            api.authority_for(firm.clients[0], "SP-DAS", today=TODAY).reason
            == "nao_exigida"
        )
        assert (
            api.authority_for(firm.clients[0], "SP-DAS", today=TODAY).state == "usable"
        )
        assert (
            api.authority_for(firm.clients[0], "SP-PAG", today=TODAY).state == "missing"
        )
        assert (
            api.authority_for(firm.clients[0], "/Declarar", today=TODAY).state
            == "unknown"
        )


def test_provider_403_appends_detected_absence_and_blocks_further_calls(
    firm: Firm,
) -> None:
    api = authority_api()
    client = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        api.declare_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        detected = api.record_authority_denial(
            client,
            "SP-PAG",
            source_call_ref="call-403",
            http_status=403,
            today=TODAY,
        )
        assert detected.status == "ausente_detectada"
        assert detected.source_call_ref == "call-403"
        assert detected.verified_by_id is None
        assert api.authority_for(client, "SP-PAG", today=TODAY).state == "missing"
        issue = apps.get_model("integrations", "IntegrationIssue").objects.get()
        assert issue.reason == "autorizacao_ausente"
        assert issue.owner_id == firm.owner.pk
        assert issue.review_on == TODAY


def test_after_revocation_a_new_declaration_waits_for_verification(firm: Firm) -> None:
    api = authority_api()
    client = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        api.declare_authority(
            firm.owner,
            client.pk,
            service="sitfis",
            code="00002",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        api.revoke_authority(
            firm.owner,
            client.pk,
            service="sitfis",
            expected_version=1,
            today=TODAY,
        )
        pending = api.declare_authority(
            firm.owner,
            client.pk,
            service="sitfis",
            code="00002",
            valid_from=TODAY,
            expected_version=2,
            today=TODAY,
        )
        assert pending.status == "verificacao_pendente"
        decision = api.authority_for(client, "SP-SIT-P", today=TODAY)
        assert (decision.state, decision.reason) == ("unknown", "verificacao_pendente")
        verified = api.verify_authority(
            firm.owner,
            client.pk,
            service="sitfis",
            expected_version=3,
            today=TODAY,
        )
        assert verified.status == "ativa"
        assert api.authority_for(client, "SP-SIT-R", today=TODAY).state == "usable"
        with pytest.raises(InvalidTransitionError):
            api.verify_authority(
                firm.owner, client.pk, service="sitfis", expected_version=4, today=TODAY
            )
        with pytest.raises(InvalidTransitionError):
            api.revoke_authority(
                firm.owner,
                client.pk,
                service="pagtoweb",
                expected_version=0,
                today=TODAY,
            )


def test_a_usable_declaration_resolves_the_owned_absence(firm: Firm) -> None:
    api = authority_api()
    client = firm.clients[0]
    issues = apps.get_model("integrations", "IntegrationIssue").objects
    with tenant_context(firm.tenant.pk):
        api.record_authority_denial(
            client,
            "SP-PAG",
            source_call_ref="call-1",
            http_status=403,
            today=TODAY,
        )
        api.record_authority_denial(
            client,
            "SP-SIT-P",
            source_call_ref="call-2",
            http_status=403,
            today=TODAY,
        )
        assert issues.get(service="pagtoweb").state == "aberta"
        api.declare_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=TODAY,
            expected_version=1,
            today=TODAY,
        )
        assert issues.get(service="pagtoweb").state == "aberta"
        api.verify_authority(
            firm.owner,
            client.pk,
            service="pagtoweb",
            expected_version=2,
            today=TODAY,
        )
        assert issues.get(service="pagtoweb").state == "resolvida"
        assert issues.get(service="sitfis").state == "aberta"


def test_a_denial_needs_a_403_on_an_authority_bound_operation(firm: Firm) -> None:
    api = authority_api()
    client = firm.clients[0]
    records = apps.get_model("integrations", "AuthorityRecord").objects
    with tenant_context(firm.tenant.pk):
        for operation, status in (("SP-PAG", 500), ("SP-DAS", 403), ("FC-X", 403)):
            with pytest.raises(ValueError, match=r"403|autoriza"):
                api.record_authority_denial(
                    client,
                    operation,
                    source_call_ref="call",
                    http_status=status,
                    today=TODAY,
                )
        assert not records.exists()


def test_two_callers_cannot_replace_the_same_revision(firm: Firm) -> None:
    api = authority_api()
    with tenant_context(firm.tenant.pk):
        api.declare_authority(
            firm.owner,
            firm.clients[0].pk,
            service="sitfis",
            code="00002",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        before = Event.objects.count()
        with pytest.raises(IntegrationConflictError):
            api.declare_authority(
                firm.owner,
                firm.clients[0].pk,
                service="sitfis",
                code="00002",
                valid_from=TODAY,
                expected_version=0,
                today=TODAY,
            )
        assert Event.objects.count() == before
        assert apps.get_model("integrations", "AuthorityRecord").objects.count() == 1


def test_unassigned_staff_cannot_write_or_discover_the_authority(firm: Firm) -> None:
    api = authority_api()
    with tenant_context(firm.tenant.pk):
        before = Event.objects.count()
        with pytest.raises(Http404):
            api.declare_authority(
                firm.accountant,
                firm.clients[0].pk,
                service="sitfis",
                code="00002",
                valid_from=TODAY,
                expected_version=0,
                today=TODAY,
            )
        assert Event.objects.count() == before
        assert not apps.get_model("integrations", "AuthorityRecord").objects.exists()


def test_revoked_capability_is_rechecked_inside_the_write(
    firm: Firm, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = authority_api()
    answers = iter([True, False])
    monkeypatch.setattr(api, "can", lambda *args, **kwargs: next(answers))
    with tenant_context(firm.tenant.pk):
        before = Event.objects.count()
        with pytest.raises(PermissionDenied):
            api.declare_authority(
                firm.owner,
                firm.clients[0].pk,
                service="sitfis",
                code="00002",
                valid_from=TODAY,
                expected_version=0,
                today=TODAY,
            )
        assert Event.objects.count() == before
        assert not apps.get_model("integrations", "AuthorityRecord").objects.exists()


@pytest.mark.parametrize("code", ["00004", "nao_exigida", "00002<script>"])
def test_catalog_code_cannot_expand_the_requested_scope(firm: Firm, code: str) -> None:
    api = authority_api()
    with tenant_context(firm.tenant.pk), pytest.raises(ValidationError):
        api.declare_authority(
            firm.owner,
            firm.clients[0].pk,
            service="sitfis",
            code=code,
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )


def test_authority_rows_cannot_be_rewritten_even_by_raw_update(firm: Firm) -> None:
    api = authority_api()
    with tenant_context(firm.tenant.pk):
        record = api.declare_authority(
            firm.owner,
            firm.clients[0].pk,
            service="sitfis",
            code="00002",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            type(record).objects.filter(pk=record.pk).update(status="revogada")
        record.refresh_from_db()
        assert record.status == "ativa"


def test_provider_certificate_drift_blocks_capture_and_opens_owned_work(
    firm: Firm,
) -> None:
    api = authority_api()
    client = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=firm.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=firm.tenant,
            client=client,
            connection=connection,
            provider_company_ref="empresa-008",
            streams=["nfe_recebidas"],
            status="ativa",
            credential_state="ativa",
            credential_generation=1,
        )
        api.declare_authority(
            firm.owner,
            client.pk,
            service="captura_nfe",
            code="autorizacao_cliente",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        api.record_certificate_status(
            link.pk,
            expected_version=1,
            certificate_cnpj=firm.clients[1].cnpj,
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=90),
            credential_generation=1,
            today=TODAY,
        )
        link.refresh_from_db()
        assert link.credential_state == "divergente"
        assert api.authority_for(client, "FC-NFE", today=TODAY).state == "missing"
        issue = apps.get_model("integrations", "IntegrationIssue").objects.get(
            reason="credencial_divergente",
        )
        assert issue.owner_id == firm.owner.pk
