"""Build one demonstrable accounting firm from nothing: `manage.py seed_demo`.

**This extends plain `BaseCommand`, not the house `TenantAwareBaseCommand`, and the
inversion is the point.** That base requires `--tenant` naming an *already active*
tenant and raises `CommandError` when none matches (`apps/core/management/base.py`).
The precondition it enforces is exactly the state this command exists to produce: it
is the first thing run against a fresh staging box, it must work with zero arguments,
and it creates the firm it then works inside. So the tenant context is entered by
hand, once the firm exists, rather than inherited from a base that would refuse to
start.

**Every write is `get_or_create`.** A staging box is reset and re-seeded, a deploy
re-runs this, and an operator who lost the output will run it again. Convergence on
the same firm — never a second copy of it, and never a rotated credential — is the
whole contract.

**Passwords and TOTP secrets are printed, once, and never stored recoverably.** They
go to stdout rather than to a file because the operator running the command is the
only intended reader and a file would leave them at rest on a shared host. A re-run
issues nothing, so the first run's output is the only copy that will ever exist.

The TOTP enrolment is not optional decoration. Creating the OWNER membership is
itself what makes `requires_mfa` true for these accounts, so a seed that stopped at
the membership would leave both operators diverted to the enrolment page before they
reached a single screen.
"""

import secrets
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation
from apps.obligations.rules import DueRuleCache
from apps.tenants.models import Membership, Tenant, TenantRole

DEFAULT_TENANT_SLUG: Final = "demo"
DEFAULT_OWNER_EMAILS: Final = "owner@demo.example.com,socia@demo.example.com"

# 18 bytes of entropy, urlsafe-encoded to 24 characters. Long enough that the printed
# value is not worth attacking, short enough that an operator can retype it.
PASSWORD_BYTES: Final = 18

CALENDAR_START: Final = date(2026, 7, 1)
CLIENT_OPENED_ON: Final = date(2020, 1, 1)


@dataclass(frozen=True, slots=True)
class DemoClient:
    """One MEI company the demo firm keeps books for."""

    legal_name: str
    cnpj: str


# Three fixed, valid CNPJs in the three spellings this product has to handle: the
# official RFB alphanumeric vector, a legacy numeric number, and one whose leading
# zeros are what a spreadsheet destroys. Fixed rather than generated so the seeded
# firm is byte-identical on every box, and so a demo exercises all three formats.
#
# The count is a constant rather than an option for the same reason: a `--clients=N`
# would make "36 obligations" a number nobody could assert.
DEMO_CLIENTS: Final[tuple[DemoClient, ...]] = (
    DemoClient("Alfanumérica ME", "12ABC34501DE35"),
    DemoClient("Legada ME", "11222333000181"),
    DemoClient("Zeros à Esquerda S.A.", "00000000000191"),
)


@dataclass(frozen=True, slots=True)
class Credential:
    """One secret issued on this run, and the only time it will ever be readable."""

    email: str
    kind: str
    value: str

    def __str__(self) -> str:
        """Render the line an operator copies into a password manager."""
        return f"  {self.email}  {self.kind}={self.value}"


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What the firm looks like once the seed has converged on it."""

    tenant_slug: str
    owners: int
    memberships: int
    clients: int
    obligations: int
    credentials: tuple[Credential, ...]


def _tenant_for(slug: str) -> Tenant:
    """Return the demo firm, refusing a slug the model itself would reject.

    `get_or_create` writes straight through `save()`, which runs no validator at all,
    so a slug ending in `-portal` would be stored silently and then serve another
    firm's portal traffic. Validating the candidate first turns that into a refusal
    before anything is written.
    """
    name = slug.replace("-", " ").title()
    candidate = Tenant(name=name, slug=slug, is_active=True)
    try:
        candidate.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as error:
        msg = f"Refusing --tenant-slug {slug!r}: {' '.join(error.messages)}"
        raise CommandError(msg) from error

    # PLATFORM_QUERY_OK: creates the very firm this command exists to produce, for an
    # operator who already holds shell access to the host — a strictly wider
    # capability than reading or writing one firm's rows.
    tenant, _created = Tenant.objects.get_or_create(
        slug=slug,
        defaults={"name": name, "is_active": True},
    )
    return tenant


def _seed_owner(tenant: Tenant, email: str) -> list[Credential]:
    """Give one operator an account, a second factor, and the whole firm to look at."""
    issued: list[Credential] = []

    user = User.objects.filter(email=email).first()
    if user is None:
        password = secrets.token_urlsafe(PASSWORD_BYTES)
        user = User.objects.create_user(
            email=email,
            password=password,
            full_name=email.split("@", maxsplit=1)[0].replace(".", " ").title(),
        )
        issued.append(Credential(email, "password", password))

    enrolled = Authenticator.objects.filter(
        user=user,
        type=Authenticator.Type.TOTP,
    ).exists()
    if not enrolled:
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
        issued.append(Credential(email, "totp-secret", secret))

    # OWNER with no client, and both halves are load-bearing. Only a firm-wide role is
    # handed the entire portfolio (`apps/authz/portfolio.py`), which is what makes the
    # demo dashboard show all three clients; and the
    # membership_role_matches_client_scope CHECK requires a firm role to carry no
    # client, so a client id here would be an IntegrityError rather than a narrower
    # grant.
    # PLATFORM_QUERY_OK: writes the membership for the account this command just
    # created, in the firm it just created, on behalf of a host-level operator.
    Membership.objects.get_or_create(
        user=user,
        tenant=tenant,
        client=None,
        defaults={"role": TenantRole.OWNER, "is_active": True},
    )
    return issued


def _seed_portfolio(tenant: Tenant) -> tuple[int, int]:
    """Register the demo clients, give each a year of DAS, and count what is there."""
    # One resolver for the whole seed rather than one per client: the eras are
    # platform reference data with no tenant dimension, so re-reading them per client
    # is precisely the N+1 the queue budgets forbid.
    rules = DueRuleCache()
    with tenant_context(tenant.id):
        for demo in DEMO_CLIENTS:
            # Keyed on the name AND the number deliberately. A CNPJ already registered
            # to a different company is a real conflict — the demo may not claim
            # somebody else's number — and the per-tenant unique index says so loudly
            # here rather than letting the seed silently adopt their row.
            client, _created = ClientCompany.objects.get_or_create(
                legal_name=demo.legal_name,
                cnpj=demo.cnpj,
                defaults={
                    "tenant": tenant,
                    "opened_on": CLIENT_OPENED_ON,
                    "is_mei": True,
                },
            )
            generate_das_calendar(client, starting_from=CALENDAR_START, rules=rules)
        return ClientCompany.objects.count(), Obligation.objects.count()


def _owner_emails(raw: str) -> list[str]:
    emails = [candidate.strip() for candidate in raw.split(",") if candidate.strip()]
    if not emails:
        msg = "--owner-emails must name at least one address."
        raise CommandError(msg)
    return emails


class Command(BaseCommand):
    """Create or converge on the demo firm, and print any credentials it issued."""

    help = "Seed a demonstrable accounting firm: one tenant, two owners, three clients."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the two selectors, both defaulted so a bare run works."""
        parser.add_argument(
            "--tenant-slug",
            dest="tenant_slug",
            default=DEFAULT_TENANT_SLUG,
            help=f"Slug of the demo firm (default: {DEFAULT_TENANT_SLUG}).",
        )
        parser.add_argument(
            "--owner-emails",
            dest="owner_emails",
            default=DEFAULT_OWNER_EMAILS,
            help="Comma-separated addresses to create as firm owners.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002
        """Seed the firm in one transaction, then report what it converged on."""
        emails = _owner_emails(options["owner_emails"])

        # One transaction for the whole seed. A CNPJ conflict half way through must
        # leave NOTHING behind — a box holding two owner accounts and one client's
        # calendar is harder to diagnose than an empty one, and the operator would
        # have already been shown passwords for accounts that survived.
        with transaction.atomic():
            tenant = _tenant_for(options["tenant_slug"])
            credentials = [
                credential
                for email in emails
                for credential in _seed_owner(tenant, email)
            ]
            clients, obligations = _seed_portfolio(tenant)
            # Counted from the rows rather than from `len(emails)`: a summary derived
            # from the seed's intent would report a healthy firm for a run that wrote
            # nothing. Firm-side only — a portal identity in this tenant is not an
            # owner, and counting one would overstate who can see the whole portfolio.
            # PLATFORM_QUERY_OK: counts the memberships this command just wrote, in
            # the firm it just created, for the operator who ran it.
            memberships = Membership.objects.filter(
                tenant=tenant,
                client__isnull=True,
            ).count()

        self._report(
            SeedResult(
                tenant_slug=tenant.slug,
                owners=len(emails),
                memberships=memberships,
                clients=clients,
                obligations=obligations,
                credentials=tuple(credentials),
            ),
        )

    def _report(self, result: SeedResult) -> None:
        """Print the summary, then any secrets — after the transaction committed."""
        self.stdout.write(self.style.SUCCESS("Demo firm ready."))
        self.stdout.write("")
        self.stdout.write(f"  tenant        {result.tenant_slug}")
        self.stdout.write(f"  owners        {result.owners}")
        self.stdout.write(f"  memberships   {result.memberships}")
        self.stdout.write(f"  clients       {result.clients}")
        self.stdout.write(f"  obligations   {result.obligations}")
        self.stdout.write("")

        if not result.credentials:
            self.stdout.write(
                "No new credentials: every account already existed, and this command "
                "never rotates one. The secrets issued on the first run remain the "
                "only ones that work.",
            )
            return

        self.stdout.write(
            self.style.WARNING(
                "Credentials issued on this run — shown ONCE and not persisted. "
                "Store them in a password manager now; re-running this command will "
                "not show them again.",
            ),
        )
        for credential in result.credentials:
            self.stdout.write(str(credential))
