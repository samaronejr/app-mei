"""Refusals preserve their meaning as well as preventing provider traffic."""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.authority import (
    authority_subject,
    declare_authority,
    head_record,
    revoke_authority,
    verify_authority,
)
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.serpro import sweeps
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.termo_state import termo_head
from tests.integrations.pagtoweb_support import World, world

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)
WINDOW = PaymentRange(date(2026, 9, 1), date(2026, 10, 21))


@pytest.mark.parametrize("service", ["pagtoweb", "sitfis"])
def test_missing_authority_identifies_the_service_and_its_owned_subject(
    world: World,
    service: str,
) -> None:
    client = world.termo.client
    with tenant_context(client.tenant_id):
        if service == "pagtoweb":
            head = head_record(client.pk, service)
            assert head is not None
            revoke_authority(
                world.termo.actor,
                client.pk,
                service=service,
                expected_version=head.version,
            )
        result = (
            sweeps.payments_for(client, WINDOW)
            if service == "pagtoweb"
            else sweeps.report_for(client)
        )
        assert result is sweeps.Outcome.REFUSED
        issue = IntegrationIssue.objects.get(client=client)
        assert issue.reason == "autorizacao_ausente"
        assert issue.service == service
        assert issue.evidence_refs == [str(authority_subject(client.pk, service))]
        assert not PaymentQuery.objects.exists()
    assert world.sent() == []


def test_a_link_removed_after_selection_is_an_ordinary_refusal(world: World) -> None:
    client = world.termo.client
    with tenant_context(client.tenant_id):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(status="revogada")
        error = None
        result = None
        try:
            result = sweeps.payments_for(client, WINDOW)
        except AttributeError as caught:
            error = caught
        assert error is None, "a vanished link must refuse without dereferencing it"
        assert result is sweeps.Outcome.REFUSED
        assert not PaymentQuery.objects.exists()
        assert not IntegrationIssue.objects.exists()
    assert world.sent() == []


def test_a_termo_that_expired_after_selection_creates_no_query(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = world.termo.client
    with tenant_context(client.tenant_id):
        head = termo_head(world.termo.connection.pk)
        assert head is not None
        assert head.valid_until is not None
        after = head.valid_until + timedelta(days=1)
        monkeypatch.setattr(timezone, "localdate", lambda: after)
        assert sweeps.payments_for(client, WINDOW) is sweeps.Outcome.REFUSED
        assert not PaymentQuery.objects.exists()
        assert not IntegrationIssue.objects.exists()
    assert world.sent() == []


def test_not_yet_valid_authority_is_not_reported_as_missing(world: World) -> None:
    client = world.termo.client
    with tenant_context(client.tenant_id):
        head = head_record(client.pk, "pagtoweb")
        assert head is not None
        revoke_authority(
            world.termo.actor,
            client.pk,
            service="pagtoweb",
            expected_version=head.version,
        )
        head = head_record(client.pk, "pagtoweb")
        assert head is not None
        declare_authority(
            world.termo.actor,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=timezone.localdate() + timedelta(days=30),
            expected_version=head.version,
        )
        head = head_record(client.pk, "pagtoweb")
        assert head is not None
        verify_authority(
            world.termo.actor,
            client.pk,
            service="pagtoweb",
            expected_version=head.version,
        )
        assert sweeps.payments_for(client, WINDOW) is sweeps.Outcome.REFUSED
        assert not IntegrationIssue.objects.exists()
        assert not PaymentQuery.objects.exists()
    assert world.sent() == []


def test_a_non_authority_report_refusal_does_not_invent_missing_authority(
    world: World,
) -> None:
    client = world.termo.client
    with tenant_context(client.tenant_id):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(status="revogada")
        assert sweeps.report_for(client) is sweeps.Outcome.REFUSED
        assert not IntegrationIssue.objects.exists()
    assert world.sent() == []
