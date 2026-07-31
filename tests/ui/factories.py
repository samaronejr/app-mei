"""One firm, its people and its portfolio — built the way the product builds them.

Every firm-side account is enrolled in TOTP and signs in through the real allauth
flow. Both matter. `MFAEnforcementMiddleware` diverts anyone holding a `Membership`
to enrolment before they reach any view, so a fixture that skips it makes each view
test assert against a redirect instead of the behaviour it names; and `force_login`
writes the session key directly, so allauth never records when authentication
happened and treats the next sensitive action as stale.
"""

from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256

from allauth.account.models import EmailAddress
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    ClientStatus,
)
from apps.core.tenancy import tenant_context
from apps.fiscal.validators import cnpj_check_digits
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp, totp_code

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
HTTP_FOUND = 302


def make_cnpj(base: str) -> str:
    """Return a checksum-valid CNPJ from a twelve-character base."""
    return base + cnpj_check_digits(base)


@dataclass
class Firm:
    """A tenant, the accounts inside it, and the clients it keeps books for."""

    tenant: Tenant
    owner: User
    accountant: User
    clients: list[ClientCompany] = field(default_factory=list)

    @property
    def host(self) -> str:
        """The subdomain `TenantMiddleware` resolves this firm from."""
        return f"{self.tenant.slug}.localhost"

    def sign_in(self, user: User) -> Client:
        """Drive the real login and second-factor flow on this firm's subdomain."""
        client = Client(SERVER_NAME=self.host)
        response = client.post(
            reverse("account_login"),
            {"login": user.email, "password": PASSWORD},
        )
        assert response.status_code == HTTP_FOUND, (
            f"password rejected for {user.email}: {response.status_code}"
        )
        response = client.post(reverse("mfa_authenticate"), {"code": totp_code()})
        assert response.status_code == HTTP_FOUND, "TOTP code was not accepted"
        return client

    def as_owner(self) -> Client:
        """A signed-in session for the firm owner."""
        return self.sign_in(self.owner)

    def as_accountant(self) -> Client:
        """A signed-in session for the staff accountant."""
        return self.sign_in(self.accountant)


def add_member(
    tenant: Tenant,
    email: str,
    role: str,
    *,
    client: ClientCompany | None = None,
) -> User:
    """Create an account with a verified address and TOTP enrolled.

    `client` is required for the two client-side roles and forbidden for the firm-side
    ones -- `membership_role_matches_client_scope` rejects either mistake -- so it is
    passed through rather than defaulted per role.
    """
    user = User.objects.create_user(email=email, password=PASSWORD)
    Membership.objects.create(user=user, tenant=tenant, role=role, client=client)
    EmailAddress.objects.get_or_create(
        user=user,
        email=user.email,
        defaults={"verified": True, "primary": True},
    )
    enrol_totp(user)
    return user


def add_client(
    firm: Firm,
    *,
    legal_name: str,
    base: str,
    status: str = ClientStatus.ACTIVE,
    opened_on: date | None = None,
) -> ClientCompany:
    """Create one client company inside its own tenant context."""
    with tenant_context(firm.tenant.id):
        company = ClientCompany.objects.create(
            tenant=firm.tenant,
            legal_name=legal_name,
            trade_name=legal_name.split(" ", maxsplit=1)[0],
            cnpj=make_cnpj(base),
            status=status,
            opened_on=opened_on or date(2020, 3, 10),
        )
    firm.clients.append(company)
    return company


def assign(firm: Firm, company: ClientCompany, user: User) -> ClientAssignment:
    """Put a client in an accountant's book of business."""
    with tenant_context(firm.tenant.id):
        return ClientAssignment.objects.create(
            tenant=firm.tenant,
            client=company,
            user=user,
            role=AssignmentRole.PRIMARY,
        )


def make_firm(slug: str, *, client_count: int = 0) -> Firm:
    """Create a firm with an owner, a staff accountant and `client_count` clients."""
    tenant = Tenant.objects.create(name=f"{slug.title()} Contabilidade", slug=slug)
    firm = Firm(
        tenant=tenant,
        owner=add_member(tenant, f"owner@{slug}.example.com", TenantRole.OWNER),
        accountant=add_member(
            tenant,
            f"staff@{slug}.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        ),
    )
    for index in range(client_count):
        add_client(
            firm,
            legal_name=f"{slug.title()} Cliente {index + 1} MEI",
            base=client_base(index, slug),
        )
    return firm


def client_base(index: int, seed: str = "") -> str:
    """Return a distinct, non-degenerate twelve-character CNPJ base.

    The `seed` is the owning firm's slug, and it is load-bearing rather than
    decorative: without it every firm's Nth client carries the *same* CNPJ, and a
    cross-tenant assertion that searches a rendered page for another firm's document
    finds its own and reports a leak that is not there — or, worse, is written around
    until it reports nothing at all.

    A base of one repeated character is refused by `validate_cnpj` even though it
    satisfies its own checksum, so the leading digit is fixed and never varies with
    the digest.
    """
    digest = sha256(seed.encode("utf-8")).hexdigest()
    discriminator = "".join(char for char in digest if char.isdigit())[:9].ljust(9, "7")
    return f"1{discriminator}{index:02d}"


__all__ = [
    "PASSWORD",
    "Firm",
    "add_client",
    "add_member",
    "assign",
    "client_base",
    "make_cnpj",
    "make_firm",
]
