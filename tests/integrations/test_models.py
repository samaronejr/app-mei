"""AXRM-007: provider connections and client provider links, enforced in the database.

Plan A7.1: one connection per (tenant, provider, environment); one link per (tenant,
client, connection); one provider company never bound to two clients of the same
connection; `capture_start_month` is a first-of-month date that never changes once set
(INV-R11); the stream list is the closed set of two collections; `secret_ref` holds a
secret's *name*, never its value (INV-R7). Each rule is asserted against the database,
not the form layer, so a raw `.create()` or `.update()` cannot slip past it.

State changes go through `apps.integrations.services`: a locked, version-checked
transition along the A7.3 machines, with no write at all on a stale version or a
transition the machine does not allow.
"""

import datetime as dt
from dataclasses import dataclass

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, InternalError, transaction

from apps.audit.models import Event
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.models import (
    ClientProviderLink,
    CredentialState,
    DisablementState,
    LinkStatus,
    ProviderConnection,
)
from apps.integrations.services import (
    CaptureStartImmutableError,
    IntegrationConflictError,
    InvalidTransitionError,
    LinkMachine,
    set_capture_start_month,
    transition_connection_status,
    transition_link,
)
from apps.integrations.validators import validate_secret_ref
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db

NOVEMBER = dt.date(2026, 11, 1)
JWT_SHAPED_VALUE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"


@dataclass
class World:
    tenant: Tenant
    client: ClientCompany
    sibling: ClientCompany
    connection: ProviderConnection


def _client(tenant: Tenant, cnpj: str, name: str) -> ClientCompany:
    with tenant_context(tenant.pk):
        return ClientCompany.objects.create(
            tenant=tenant, legal_name=name, cnpj=cnpj, is_mei=True
        )


def _world(slug: str) -> World:
    tenant = Tenant.objects.create(name=slug.title(), slug=slug)
    client = _client(tenant, "11222333000181", f"CLIENTE {slug.upper()}")
    sibling = _client(tenant, "11444777000161", f"IRMA {slug.upper()}")
    with tenant_context(tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=tenant, provider="focus", environment="fake"
        )
    return World(tenant, client, sibling, connection)


@pytest.fixture
def world() -> World:
    return _world("acme")


def _link(world: World, **fields: object) -> ClientProviderLink:
    values: dict[str, object] = {
        "tenant": world.tenant,
        "client": world.client,
        "connection": world.connection,
        "provider_company_ref": "empresa-1",
    }
    values.update(fields)
    with tenant_context(world.tenant.pk):
        return ClientProviderLink.objects.create(**values)


@pytest.fixture
def link(world: World) -> ClientProviderLink:
    return _link(world)


def test_new_rows_start_closed(world: World, link: ClientProviderLink) -> None:
    assert world.connection.status == "inativa"
    assert world.connection.secret_ref == ""
    assert world.connection.version == 1
    assert link.status == LinkStatus.CONFIGURACAO_PENDENTE
    assert link.credential_state == CredentialState.AUSENTE
    assert link.disablement_state == DisablementState.ATIVA
    assert link.streams == []
    assert link.capture_start_month is None
    assert link.credential_generation == 0
    assert link.version == 1


def test_one_connection_per_tenant_provider_and_environment(world: World) -> None:
    # _world gives the second firm its own focus/fake connection: same provider and
    # environment, a different firm, so a different row.
    other = _world("outra")
    assert other.connection.environment == world.connection.environment
    with (
        tenant_context(world.tenant.pk),
        pytest.raises(IntegrityError, match="providerconnection_scope_uniq"),
        transaction.atomic(),
    ):
        ProviderConnection.objects.create(
            tenant=world.tenant, provider="focus", environment="fake"
        )


def test_one_link_per_client_and_connection(
    world: World, link: ClientProviderLink
) -> None:
    with pytest.raises(IntegrityError, match="clientproviderlink_connection_uniq"):
        _link(world, provider_company_ref="empresa-2")


def test_one_provider_company_is_never_bound_to_two_clients(
    world: World, link: ClientProviderLink
) -> None:
    with pytest.raises(IntegrityError, match="clientproviderlink_company_ref_uniq"):
        _link(world, client=world.sibling)


def test_the_provider_company_reference_is_required(world: World) -> None:
    with pytest.raises(IntegrityError, match="clientproviderlink_company_ref_present"):
        _link(world, provider_company_ref="")


def test_streams_are_the_closed_set_of_two_collections(world: World) -> None:
    accepted = _link(world, streams=["nfe_recebidas", "nfsen_recebidas"])
    assert accepted.streams == ["nfe_recebidas", "nfsen_recebidas"]
    with pytest.raises(IntegrityError, match="clientproviderlink_streams_known"):
        _link(
            world,
            client=world.sibling,
            provider_company_ref="empresa-2",
            streams=["nfse_emitidas"],
        )


def test_the_capture_start_is_the_first_day_of_a_month(world: World) -> None:
    with pytest.raises(IntegrityError, match="clientproviderlink_capture_start_month"):
        _link(world, capture_start_month=dt.date(2026, 11, 15))


def test_the_capture_start_never_changes_once_set(
    world: World, link: ClientProviderLink
) -> None:
    with tenant_context(world.tenant.pk):
        # Setting it the first time is allowed, and re-saving the same value is too.
        ClientProviderLink.objects.filter(pk=link.pk).update(
            capture_start_month=NOVEMBER
        )
        ClientProviderLink.objects.filter(pk=link.pk).update(
            capture_start_month=NOVEMBER, version=2
        )
        # A raw UPDATE that moves it -- earlier (backfill) or to NULL -- is refused
        # by the database, not merely by the service.
        for moved in (dt.date(2026, 10, 1), None):
            with (
                pytest.raises((IntegrityError, InternalError), match="imutavel"),
                transaction.atomic(),
            ):
                ClientProviderLink.objects.filter(pk=link.pk).update(
                    capture_start_month=moved
                )
        link.refresh_from_db()
    assert link.capture_start_month == NOVEMBER


def test_set_capture_start_month_writes_once_then_refuses(
    world: World, link: ClientProviderLink
) -> None:
    updated = set_capture_start_month(
        world.tenant.pk, link.pk, month=NOVEMBER, expected_version=1
    )
    assert updated.capture_start_month == NOVEMBER
    assert updated.version == 2

    with pytest.raises(CaptureStartImmutableError):
        set_capture_start_month(
            world.tenant.pk, link.pk, month=dt.date(2026, 9, 1), expected_version=2
        )
    with tenant_context(world.tenant.pk):
        link.refresh_from_db()
    assert link.capture_start_month == NOVEMBER
    assert link.version == 2


def test_set_capture_start_month_refuses_a_mid_month_date(
    world: World, link: ClientProviderLink
) -> None:
    with pytest.raises(ValidationError):
        set_capture_start_month(
            world.tenant.pk,
            link.pk,
            month=dt.date(2026, 11, 2),
            expected_version=1,
        )


def _events(tenant: Tenant) -> int:
    with tenant_context(tenant.pk):
        return Event.objects.filter(action__startswith="integrations.").count()


def test_a_link_transition_follows_the_machine_and_bumps_the_version(
    world: World, link: ClientProviderLink
) -> None:
    moved = transition_link(
        world.tenant.pk,
        link.pk,
        machine=LinkMachine.STATUS,
        to=LinkStatus.ATIVA,
        expected_version=1,
    )
    assert moved.status == LinkStatus.ATIVA
    assert moved.version == 2
    assert _events(world.tenant) == 1


def test_a_stale_version_writes_nothing(world: World, link: ClientProviderLink) -> None:
    # Two callers rendered the same version; the first wins.
    transition_link(
        world.tenant.pk,
        link.pk,
        machine=LinkMachine.STATUS,
        to=LinkStatus.ATIVA,
        expected_version=1,
    )
    with pytest.raises(IntegrationConflictError):
        transition_link(
            world.tenant.pk,
            link.pk,
            machine=LinkMachine.STATUS,
            to=LinkStatus.REVOGADA,
            expected_version=1,
        )
    with tenant_context(world.tenant.pk):
        link.refresh_from_db()
    assert link.status == LinkStatus.ATIVA
    assert link.version == 2
    assert _events(world.tenant) == 1


def test_a_transition_the_machine_does_not_allow_writes_nothing(
    world: World, link: ClientProviderLink
) -> None:
    with pytest.raises(InvalidTransitionError):
        transition_link(
            world.tenant.pk,
            link.pk,
            machine=LinkMachine.CREDENTIAL,
            to=CredentialState.ATIVA,
            expected_version=1,
        )
    with pytest.raises(InvalidTransitionError):
        transition_link(
            world.tenant.pk,
            link.pk,
            machine=LinkMachine.DISABLEMENT,
            to=DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA,
            expected_version=1,
        )
    with tenant_context(world.tenant.pk):
        link.refresh_from_db()
    assert link.credential_state == CredentialState.AUSENTE
    assert link.disablement_state == DisablementState.ATIVA
    assert link.version == 1
    assert _events(world.tenant) == 0


def test_a_revoked_link_stays_revoked(world: World, link: ClientProviderLink) -> None:
    transition_link(
        world.tenant.pk,
        link.pk,
        machine=LinkMachine.STATUS,
        to=LinkStatus.REVOGADA,
        expected_version=1,
    )
    with pytest.raises(InvalidTransitionError):
        transition_link(
            world.tenant.pk,
            link.pk,
            machine=LinkMachine.STATUS,
            to=LinkStatus.ATIVA,
            expected_version=2,
        )


def test_rotation_back_to_active_starts_a_new_credential_generation(
    world: World, link: ClientProviderLink
) -> None:
    version = 1
    for state in (
        CredentialState.CONFIRMACAO_PENDENTE,
        CredentialState.ATIVA,
        CredentialState.ROTACAO_PENDENTE,
        CredentialState.ATIVA,
    ):
        link = transition_link(
            world.tenant.pk,
            link.pk,
            machine=LinkMachine.CREDENTIAL,
            to=state,
            expected_version=version,
        )
        version = link.version
    # Only the rotation's return to `ativa` names a new certificate generation.
    assert link.credential_generation == 1
    assert link.credential_state == CredentialState.ATIVA


def test_the_disablement_chain_runs_in_order(
    world: World, link: ClientProviderLink
) -> None:
    version = 1
    for state in (
        DisablementState.PAUSA_SOLICITADA,
        DisablementState.PAUSADA_LOCALMENTE,
        DisablementState.DESATIVACAO_NO_PROVEDOR_PENDENTE,
        DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA,
    ):
        link = transition_link(
            world.tenant.pk,
            link.pk,
            machine=LinkMachine.DISABLEMENT,
            to=state,
            expected_version=version,
        )
        version = link.version
    assert link.disablement_state == DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA


def test_another_firms_link_is_not_found_and_not_written(
    world: World, link: ClientProviderLink
) -> None:
    other = _world("intrusa")
    with pytest.raises(ClientProviderLink.DoesNotExist):
        transition_link(
            other.tenant.pk,
            link.pk,
            machine=LinkMachine.STATUS,
            to=LinkStatus.ATIVA,
            expected_version=1,
        )
    with tenant_context(world.tenant.pk):
        link.refresh_from_db()
    assert link.status == LinkStatus.CONFIGURACAO_PENDENTE
    assert _events(other.tenant) == 0


def test_a_connection_status_change_is_version_checked(world: World) -> None:
    moved = transition_connection_status(
        world.tenant.pk, world.connection.pk, to="pausada", expected_version=1
    )
    assert moved.status == "pausada"
    assert moved.version == 2
    with pytest.raises(IntegrationConflictError):
        transition_connection_status(
            world.tenant.pk, world.connection.pk, to="ativa", expected_version=1
        )
    with pytest.raises(InvalidTransitionError):
        transition_connection_status(
            world.tenant.pk, world.connection.pk, to="pausada", expected_version=2
        )
    with tenant_context(world.tenant.pk):
        world.connection.refresh_from_db()
    assert world.connection.status == "pausada"
    assert world.connection.version == 2


@pytest.mark.parametrize(
    "value",
    [
        "serpro_contratante",
        "focus/token_master",
        "providers.serpro-consumer",
        "",
    ],
)
def test_secret_ref_accepts_a_secret_name(value: str) -> None:
    validate_secret_ref(value)


@pytest.mark.parametrize(
    "value",
    [
        # A JWT, a PEM header, a hex digest, a base64 run, a bearer header and a
        # URL with an embedded credential: each is a value, never a name.
        JWT_SHAPED_VALUE,
        "-----BEGIN PRIVATE KEY-----",
        "3f786850e387550fdab836ed7e6dc881de23001b",
        "dGhpcyBpcyBhIHNlY3JldCB2YWx1ZQ",
        "Bearer abc",
        "https://user:pass@example.com",
        "a" * 65,
    ],
)
def test_secret_ref_refuses_anything_that_looks_like_a_value(value: str) -> None:
    with pytest.raises(ValidationError, match="nome do segredo"):
        validate_secret_ref(value)


def test_the_database_refuses_a_secret_value_written_around_the_validator(
    world: World,
) -> None:
    with (
        tenant_context(world.tenant.pk),
        pytest.raises(IntegrityError, match="providerconnection_secret_ref_is_a_name"),
        transaction.atomic(),
    ):
        ProviderConnection.objects.filter(pk=world.connection.pk).update(
            secret_ref=JWT_SHAPED_VALUE
        )
