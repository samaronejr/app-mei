"""The acceptance transaction binds a token to the caller's expected door."""

import inspect
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Final, cast

import pytest
from allauth.account.models import EmailAddress
from django.db import close_old_connections, connections

from apps.accounts import invites as invite_domain
from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.django_db(transaction=True)

INVITED: Final = "dona@padaria.example"
NOT_FOUND_SENTENCE: Final = "No invitation matches that link."
PADARIA_CNPJ: Final = "11222333000181"
PASSWORD: Final = "correct-horse-battery-staple"  # noqa: S105


@pytest.fixture
def firm() -> Tenant:
    return Tenant.objects.create(name="Acme Contabilidade", slug="acme")


@pytest.fixture
def padaria(firm: Tenant) -> ClientCompany:
    with tenant_context(firm.pk):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        return ClientCompany.all_objects.create(
            tenant=firm,
            legal_name="PADARIA DA ESQUINA MEI",
            cnpj=PADARIA_CNPJ,
            is_mei=True,
        )


def _verified_invitee() -> User:
    user = User.objects.create_user(email=INVITED)
    EmailAddress.objects.create(
        user=user,
        email=INVITED,
        verified=True,
        primary=True,
    )
    return user


def _client_invitation(firm: Tenant, padaria: ClientCompany) -> Invite:
    invite, _raw_token = Invite.issue(
        tenant=firm,
        email=INVITED,
        role=TenantRole.CLIENT_OWNER,
        client=padaria,
    )
    return invite


def _firm_invitation(firm: Tenant) -> Invite:
    invite, _raw_token = Invite.issue(
        tenant=firm,
        email=INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    return invite


def _acceptance_events(invite: Invite) -> PlatformEvent:
    return PlatformEvent.objects.get(
        action=AuditAction.INVITE_ACCEPTED,
        metadata__invite_id=str(invite.pk),
    )


def test_a_direct_caller_cannot_accept_a_client_invite_as_firm_scope(
    firm: Tenant,
    padaria: ClientCompany,
) -> None:
    invite = _client_invitation(firm, padaria)
    invitee = _verified_invitee()
    call = cast("Callable[..., Membership]", invite_domain.accept_invite)

    try:
        leaked = call(invite=invite, user=invitee)
    except TypeError:
        scope = getattr(invite_domain, "InviteScope", None)
        mismatch_error = getattr(invite_domain, "InviteScopeMismatchError", None)
        assert scope is not None, "accept_invite refuses omission but exposes no scope"
        assert isinstance(mismatch_error, type), "scope mismatch error is unavailable"
        with pytest.raises(mismatch_error) as refused:
            call(
                invite=invite,
                user=invitee,
                expected_scope=scope.FIRM,
            )
        assert str(refused.value) == NOT_FOUND_SENTENCE
    else:
        invite.refresh_from_db()
        row = (
            Membership.objects.filter(pk=leaked.pk)
            .values(
                "id",
                "role",
                "client_id",
                "is_active",
            )
            .get()
        )
        pytest.fail(
            "direct accept_invite(invite=<client-scoped>, user=...) succeeded; "
            f"leaked seat={leaked.pk}; membership row={row!r}; "
            f"accepted_at={invite.accepted_at!r}"
        )


def test_a_firm_invitation_is_refused_when_the_caller_expects_a_client(
    firm: Tenant,
) -> None:
    invite = _firm_invitation(firm)
    invitee = _verified_invitee()

    with pytest.raises(invite_domain.InviteScopeMismatchError) as refused:
        invite_domain.accept_invite(
            invite=invite,
            user=invitee,
            expected_scope=invite_domain.InviteScope.CLIENT,
        )
    assert str(refused.value) == NOT_FOUND_SENTENCE


def test_a_firm_invitation_accepts_when_the_caller_expects_the_firm(
    firm: Tenant,
) -> None:
    invite = _firm_invitation(firm)
    invitee = _verified_invitee()

    seat = invite_domain.accept_invite(
        invite=invite,
        user=invitee,
        expected_scope=invite_domain.InviteScope.FIRM,
    )

    assert seat.tenant_id == firm.pk
    assert seat.client_id is None
    assert seat.role == TenantRole.STAFF_ACCOUNTANT
    assert seat.is_active


def test_a_client_invitation_accepts_when_the_caller_expects_a_client(
    firm: Tenant,
    padaria: ClientCompany,
) -> None:
    invite = _client_invitation(firm, padaria)
    invitee = _verified_invitee()

    seat = invite_domain.accept_invite(
        invite=invite,
        user=invitee,
        expected_scope=invite_domain.InviteScope.CLIENT,
    )

    assert seat.tenant_id == firm.pk
    assert seat.client_id == padaria.pk
    assert seat.role == TenantRole.CLIENT_OWNER
    assert seat.is_active


def test_wrong_scope_refuses_before_mutation_and_preserves_the_token(
    firm: Tenant,
    padaria: ClientCompany,
) -> None:
    invite = _client_invitation(firm, padaria)
    invitee = _verified_invitee()

    with pytest.raises(invite_domain.InviteScopeMismatchError):
        invite_domain.accept_invite(
            invite=invite,
            user=invitee,
            expected_scope=invite_domain.InviteScope.FIRM,
        )

    invite.refresh_from_db()
    assert invite.accepted_at is None
    assert not Membership.objects.exists()
    assert not PlatformEvent.objects.filter(
        action=AuditAction.INVITE_ACCEPTED,
        metadata__invite_id=str(invite.pk),
    ).exists()

    seat = invite_domain.accept_invite(
        invite=invite,
        user=invitee,
        expected_scope=invite_domain.InviteScope.CLIENT,
    )

    assert seat.client_id == padaria.pk
    invite.refresh_from_db()
    assert invite.accepted_at is not None
    assert _acceptance_events(invite).tenant_id == firm.pk


def test_register_helper_cannot_bypass_scope_before_account_creation(
    firm: Tenant,
    padaria: ClientCompany,
) -> None:
    invite = _client_invitation(firm, padaria)

    with pytest.raises(invite_domain.InviteScopeMismatchError):
        invite_domain.register_and_accept(
            invite=invite,
            password=PASSWORD,
            expected_scope=invite_domain.InviteScope.FIRM,
        )

    invite.refresh_from_db()
    assert invite.accepted_at is None
    assert not User.objects.filter(email=INVITED).exists()
    assert not EmailAddress.objects.filter(email=INVITED).exists()
    assert not Membership.objects.exists()
    assert not PlatformEvent.objects.filter(
        action=AuditAction.INVITE_ACCEPTED,
        metadata__invite_id=str(invite.pk),
    ).exists()

    user, seat = invite_domain.register_and_accept(
        invite=invite,
        password=PASSWORD,
        expected_scope=invite_domain.InviteScope.CLIENT,
    )

    assert user.email == INVITED
    assert seat.client_id == padaria.pk
    assert seat.role == TenantRole.CLIENT_OWNER


def test_scope_is_checked_inside_the_lock_before_the_first_mutation() -> None:
    source = inspect.getsource(invite_domain.accept_invite)
    anchors = [
        "@transaction.atomic",
        "select_for_update().get",
        "_assert_redeemable(locked)",
        "_assert_expected_scope(locked, expected_scope)",
        "_firm_seat(locked, user)",
    ]

    positions = [source.index(anchor) for anchor in anchors]

    assert positions == sorted(positions)


def test_registration_checks_scope_before_creating_the_account() -> None:
    source = inspect.getsource(invite_domain.register_and_accept)
    anchors = [
        "@transaction.atomic",
        "select_for_update().get",
        "_assert_redeemable(locked)",
        "_assert_expected_scope(locked, expected_scope)",
        "User.objects.create_user",
        "EmailAddress.objects.create",
        "accept_invite(",
    ]

    positions = [source.index(anchor) for anchor in anchors]

    assert positions == sorted(positions)


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_acceptances_create_exactly_one_membership(
    firm: Tenant,
) -> None:
    invite = _firm_invitation(firm)
    invitee = _verified_invitee()
    barrier = Barrier(2)

    def redeem() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            seat = invite_domain.accept_invite(
                invite=invite,
                user=invitee,
                expected_scope=invite_domain.InviteScope.FIRM,
            )
        except invite_domain.InviteAlreadyAcceptedError:
            return "already-accepted"
        finally:
            connections.close_all()
        return f"accepted:{seat.pk}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(redeem) for _index in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert results.count("already-accepted") == 1
    assert sum(result.startswith("accepted:") for result in results) == 1
    assert Membership.objects.filter(user=invitee, tenant=firm).count() == 1
    assert (
        PlatformEvent.objects.filter(
            action=AuditAction.INVITE_ACCEPTED,
            metadata__invite_id=str(invite.pk),
        ).count()
        == 1
    )
