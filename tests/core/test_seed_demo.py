"""The demo seed: one firm an operator can actually sign into, run twice.

Every assertion here is about **convergence**, because the command's whole job is to
be re-run. A staging box is reset and re-seeded; a deploy re-runs it; an operator who
lost the output runs it again hoping for a fresh password. The first two of those must
produce the same firm rather than a second copy of it, and the third must NOT quietly
rotate a credential someone already stored in a password manager — an account whose
password changed under them looks exactly like an account that was compromised.

The TOTP authenticator is asserted directly rather than inferred from the login flow.
Creating the OWNER membership is itself what makes `requires_mfa` true for these
accounts, so a seed that stops at the membership leaves both operators dead-ended at
the enrolment redirect the moment they sign in — a firm that looks seeded and cannot
be demonstrated.
"""

import re
from datetime import date
from io import StringIO

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth import authenticate
from django.core.management import CommandError, call_command
from django.db import IntegrityError

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db(transaction=True)

SLUG = "demo"
EXPECTED_OWNERS = 2
EXPECTED_CLIENTS = 3
EXPECTED_OBLIGATIONS = 36

# One of the three numbers the seed registers. Named here so the conflict test cannot
# drift onto a CNPJ the command never touches and pass while proving nothing.
SEEDED_CNPJ = "11222333000181"

# The rolls the business-day calendar must still be producing: 2026-09-20 is a Sunday
# and 2026-11-20 is a national holiday.
PINNED_ROLLS = {
    date(2026, 8, 1): date(2026, 9, 21),
    date(2026, 10, 1): date(2026, 11, 23),
}


def _run(*args: str) -> str:
    """Run the command and return everything it printed."""
    out = StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


def _tenant() -> Tenant:
    return Tenant.objects.get(slug=SLUG)


def _password_for(output: str, email: str) -> str:
    match = re.search(rf"{re.escape(email)}\s+password=(\S+)", output)
    assert match is not None, f"no password printed for {email}:\n{output}"
    return match.group(1)


def _emails(output: str) -> list[str]:
    return re.findall(r"(\S+@\S+)\s+password=", output)


def _obligation_rows() -> dict[tuple[str, date], date]:
    """Return every seeded obligation keyed by client and competence."""
    with tenant_context(_tenant().id):
        return {
            (legal_name, competence): resolved
            for legal_name, competence, resolved in Obligation.objects.values_list(
                "client__legal_name",
                "competence_month",
                "resolved_due_date",
            )
        }


def test_a_single_run_produces_the_whole_firm() -> None:
    # Given an empty database
    # When the command is run with no arguments at all
    _run()

    # Then one firm exists with two owners, three clients and a full year of DAS
    tenant = _tenant()
    assert tenant.is_active
    with tenant_context(tenant.id):
        assert ClientCompany.objects.count() == EXPECTED_CLIENTS
        assert Obligation.objects.count() == EXPECTED_OBLIGATIONS
    assert User.objects.count() == EXPECTED_OWNERS


def test_running_it_twice_converges_on_the_same_firm() -> None:
    # Given a seeded firm
    _run()

    # When the command runs a second time, exactly as a re-deploy runs it
    _run()

    # Then nothing is duplicated — not the tenant, not the accounts, not the calendar
    assert Tenant.objects.filter(slug=SLUG).count() == 1
    assert User.objects.count() == EXPECTED_OWNERS
    tenant = _tenant()
    assert Membership.objects.filter(tenant=tenant).count() == EXPECTED_OWNERS
    with tenant_context(tenant.id):
        assert ClientCompany.objects.count() == EXPECTED_CLIENTS
        assert Obligation.objects.count() == EXPECTED_OBLIGATIONS


def test_every_seeded_owner_carries_a_totp_authenticator() -> None:
    # Given a seeded firm
    _run()

    # When each owner's second factor is looked for
    # Then it is there. Without it the OWNER membership this command creates is what
    # makes requires_mfa true, so both accounts would be diverted to enrolment before
    # reaching a single screen.
    users = list(User.objects.all())
    assert len(users) == EXPECTED_OWNERS
    for user in users:
        enrolled = Authenticator.objects.filter(
            user=user,
            type=Authenticator.Type.TOTP,
        ).count()
        assert enrolled == 1, f"{user.email} has {enrolled} TOTP authenticators"


def test_each_owner_holds_one_firm_wide_membership() -> None:
    # Given a seeded firm
    _run()

    # When the memberships are read
    memberships = list(Membership.objects.filter(tenant=_tenant()))

    # Then both are OWNER with no client. Only a firm-wide role is handed the whole
    # portfolio, and the membership_role_matches_client_scope CHECK requires a firm
    # role to carry no client at all.
    assert len(memberships) == EXPECTED_OWNERS
    assert {membership.role for membership in memberships} == {TenantRole.OWNER}
    assert all(membership.client_id is None for membership in memberships)
    assert all(membership.is_active for membership in memberships)


def test_the_printed_passwords_actually_sign_the_owners_in() -> None:
    # Given the credentials the first run printed
    output = _run()
    emails = _emails(output)
    assert len(emails) == EXPECTED_OWNERS

    # When each is presented to the real authentication stack
    # Then it is accepted. A printed password nobody can use is the failure mode a
    # count of created users cannot see.
    for email in emails:
        signed_in = authenticate(username=email, password=_password_for(output, email))
        assert signed_in is not None, f"{email} could not sign in"
        assert signed_in.email == email


def test_a_second_run_does_not_rotate_a_password_an_operator_already_stored() -> None:
    # Given the credentials from the first run
    first = _run()
    emails = _emails(first)
    originals = {email: _password_for(first, email) for email in emails}

    # When the command is run again
    second = _run()

    # Then it issues nothing new, and the original passwords still work
    assert "password=" not in second
    assert "totp-secret=" not in second
    for email, password in originals.items():
        assert authenticate(username=email, password=password) is not None


def test_every_client_gets_the_two_pinned_rolled_deadlines() -> None:
    # Given a seeded firm
    _run()
    rows = _obligation_rows()

    # When the two competences whose nominal date is not a business day are read
    # Then every client carries the ROLLED date, for all three of them
    names = {legal_name for legal_name, _competence in rows}
    assert len(names) == EXPECTED_CLIENTS
    for name in names:
        for competence, resolved in PINNED_ROLLS.items():
            assert rows[name, competence] == resolved


def test_the_printed_obligation_count_matches_the_database() -> None:
    # Given a run
    output = _run()

    # When the number the summary reports is compared with the number of rows
    reported = re.search(r"obligations\s+(\d+)", output)
    assert reported is not None, f"no obligation count in the summary:\n{output}"
    with tenant_context(_tenant().id):
        stored = Obligation.objects.count()

    # Then they agree. A summary printed from the seed's intent rather than from the
    # rows would report a healthy 36 for a run that wrote nothing.
    assert int(reported.group(1)) == stored == EXPECTED_OBLIGATIONS


def test_a_cnpj_already_registered_to_another_company_stops_the_whole_seed() -> None:
    # Given a firm that already registered one of the seed's numbers to a DIFFERENT
    # company — the demo may not claim a CNPJ that is somebody else's
    tenant = Tenant.objects.create(name="Demo", slug=SLUG)
    with tenant_context(tenant.id):
        ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Outra ME",
            cnpj=SEEDED_CNPJ,
        )

    # When the seed runs
    with pytest.raises(IntegrityError):
        _run()

    # Then it failed loudly and wrote NOTHING — not the owners created before the
    # conflict, and not the first client's twelve obligations either
    assert User.objects.count() == 0
    assert Membership.objects.filter(tenant=tenant).count() == 0
    with tenant_context(tenant.id):
        assert ClientCompany.objects.count() == 1
        assert Obligation.objects.count() == 0


@pytest.mark.parametrize("slug", ["acme-portal", "Not A Slug"])
def test_a_slug_the_model_would_refuse_is_rejected_before_anything_is_written(
    slug: str,
) -> None:
    # Given a slug that would steal another firm's portal host, or is not a slug at
    # all. get_or_create writes straight through save(), which runs no validator, so
    # without an explicit check both would be stored silently.
    # When the command is run with it
    with pytest.raises(CommandError) as raised:
        _run(f"--tenant-slug={slug}")

    # Then the refusal names the slug it refused — an assertion on the exception TYPE
    # alone would pass on any CommandError, including the one raised when the command
    # does not exist at all — and nothing was created
    assert slug in str(raised.value)
    assert Tenant.objects.count() == 0
    assert User.objects.count() == 0
