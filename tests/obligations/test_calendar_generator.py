"""The twelve-month DAS calendar, its idempotency, and its tenant boundary.

Two properties here are database constraints rather than conventions, and both are for
the same reason: `task_acks_late = True` means a task that loses its worker mid-flight
is REQUEUED and runs again. Duplicate generation is an expected input, not a
hypothetical, so "the generator checks first" is not a guarantee — two workers can
check at the same time and both find nothing.

The composite foreign key is the other one. PostgreSQL documents that referential
integrity checks bypass row security, so a plain `client_id` foreign key proves only
that the client exists *somewhere*, which is both a cross-tenant write and an
existence oracle for another firm's primary keys.
"""

from datetime import date

import pytest
import uuid6
from django.db import IntegrityError, connection, transaction

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, ObligationStatus, ObligationType
from apps.tenants.models import Tenant
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

CALENDAR_MONTHS = 12
FROM_JULY = date(2026, 7, 1)


class Firm:
    """One tenant and one client of it."""

    def __init__(self, tenant: Tenant, client: ClientCompany) -> None:
        self.tenant = tenant
        self.client = client


def make_firm(slug: str, *, opened_on: date | None = None) -> Firm:
    """Create a tenant with a single client, inside that tenant's own context."""
    tenant = Tenant.objects.create(name=slug.title(), slug=slug)
    with tenant_context(tenant.id):
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name=f"{slug} MEI",
            cnpj="11222333000181",
            opened_on=opened_on,
        )
    return Firm(tenant, client)


@pytest.fixture
def alpha() -> Firm:
    return make_firm("alpha-gen", opened_on=date(2020, 3, 10))


@pytest.fixture
def beta() -> Firm:
    return make_firm("beta-gen", opened_on=date(2020, 3, 10))


def test_generating_yields_exactly_twelve_obligations(alpha: Firm) -> None:
    # Given a client that has been open for years
    assert_isolated_role()

    # When the calendar is generated from July 2026
    with tenant_context(alpha.tenant.id):
        created = generate_das_calendar(alpha.client, starting_from=FROM_JULY)
        rows = list(Obligation.objects.order_by("competence_month"))

    # Then there are twelve, consecutive, starting at the requested month
    assert created == CALENDAR_MONTHS
    assert len(rows) == CALENDAR_MONTHS
    assert [row.competence_month for row in rows] == [
        date(2026, 7, 1),
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2026, 11, 1),
        date(2026, 12, 1),
        date(2027, 1, 1),
        date(2027, 2, 1),
        date(2027, 3, 1),
        date(2027, 4, 1),
        date(2027, 5, 1),
        date(2027, 6, 1),
    ]


def test_the_generated_due_dates_are_the_resolved_ones(alpha: Firm) -> None:
    # Given a generated calendar
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        generate_das_calendar(alpha.client, starting_from=FROM_JULY)
        by_competence = {row.competence_month: row for row in Obligation.objects.all()}

    # When the three competences whose nominal date is not a business day are read
    # Then each carries BOTH dates: the nominal one the rule places, and the rolled
    # one the client must actually pay by
    august = by_competence[date(2026, 8, 1)]
    assert august.nominal_due_date == date(2026, 9, 20)
    assert august.resolved_due_date == date(2026, 9, 21)

    october = by_competence[date(2026, 10, 1)]
    assert october.nominal_due_date == date(2026, 11, 20)
    assert october.resolved_due_date == date(2026, 11, 23)

    # ...and an ordinary month is untouched
    june = by_competence[date(2026, 7, 1)]
    assert june.nominal_due_date == june.resolved_due_date == date(2026, 8, 20)


def test_every_generated_obligation_starts_scheduled(alpha: Firm) -> None:
    # Given a generated calendar
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        generate_das_calendar(alpha.client, starting_from=FROM_JULY)
        statuses = set(Obligation.objects.values_list("status", flat=True))

    # When the statuses are read
    # Then all are scheduled, and none is pre-marked paid
    assert statuses == {ObligationStatus.SCHEDULED}


def test_regenerating_is_a_no_op_and_raises_nothing(alpha: Firm) -> None:
    # Given a calendar that has already been generated
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        generate_das_calendar(alpha.client, starting_from=FROM_JULY)

        # When the generator runs a second time, exactly as it does after a worker
        # loses its lease and the task is redelivered
        created_again = generate_das_calendar(alpha.client, starting_from=FROM_JULY)
        total = Obligation.objects.count()

    # Then nothing is inserted, the count is still twelve rather than twenty-four,
    # and NO IntegrityError escaped. A raw violation inside the task's atomic block
    # would abort the whole transaction, so the retry would fail loudly instead of
    # being the no-op the design depends on.
    assert created_again == 0
    assert total == CALENDAR_MONTHS


def test_the_transaction_is_still_usable_after_a_duplicate_run(alpha: Firm) -> None:
    # Given a generated calendar, all inside ONE transaction
    assert_isolated_role()
    with tenant_context(alpha.tenant.id), transaction.atomic():
        generate_das_calendar(alpha.client, starting_from=FROM_JULY)
        generate_das_calendar(alpha.client, starting_from=FROM_JULY)

        # When further work is attempted in the same transaction
        still_works = Obligation.objects.count()

    # Then it succeeds. This is the assertion that separates ignore_conflicts from a
    # caught IntegrityError: the caught version leaves the transaction aborted and
    # every subsequent statement in the task fails with InFailedSqlTransaction.
    assert still_works == CALENDAR_MONTHS


def test_a_client_opened_mid_window_starts_at_its_opening_month(alpha: Firm) -> None:
    # Given a client that opened in October 2026, inside the requested window
    assert alpha is not None
    late = make_firm("late-gen", opened_on=date(2026, 10, 20))
    assert_isolated_role()

    # When the calendar is generated from July 2026
    with tenant_context(late.tenant.id):
        generate_das_calendar(late.client, starting_from=FROM_JULY)
        first = Obligation.objects.order_by("competence_month").first()
        count = Obligation.objects.count()

    # Then it starts in October, not July and certainly not January. Generating
    # obligations for months before the company existed would put overdue items on
    # an accountant's queue for a client that had not opened yet.
    assert first is not None
    assert first.competence_month == date(2026, 10, 1)
    assert count == CALENDAR_MONTHS


def test_a_client_with_no_opening_date_starts_at_the_window(alpha: Firm) -> None:
    # Given a client whose opening date has not been recorded yet
    assert alpha is not None
    unknown = make_firm("unknown-gen", opened_on=None)
    assert_isolated_role()

    # When the calendar is generated
    with tenant_context(unknown.tenant.id):
        generate_das_calendar(unknown.client, starting_from=FROM_JULY)
        first = Obligation.objects.order_by("competence_month").first()

    # Then the window start is used. Refusing to generate would be worse: an
    # incomplete registry would silently mean no deadlines at all for that client.
    assert first is not None
    assert first.competence_month == FROM_JULY


def test_one_firms_calendar_is_invisible_to_another(alpha: Firm, beta: Firm) -> None:
    # Given both firms with generated calendars
    assert_isolated_role()
    for firm in (alpha, beta):
        with tenant_context(firm.tenant.id):
            generate_das_calendar(firm.client, starting_from=FROM_JULY)

    # When Alpha counts obligations through the UNSCOPED manager and a raw cursor,
    # both of which bypass the application-layer filter
    with tenant_context(alpha.tenant.id):
        via_unscoped = Obligation.all_objects.count()
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {Obligation._meta.db_table}")  # noqa: S608
            row = cursor.fetchone()

    # Then it sees only its own twelve. Written through Obligation.objects this
    # would pass with row-level security switched off entirely.
    assert via_unscoped == CALENDAR_MONTHS
    assert row is not None
    assert row[0] == CALENDAR_MONTHS


def test_a_cross_tenant_obligation_is_rejected_by_the_composite_key(
    alpha: Firm,
    beta: Firm,
) -> None:
    # Given Alpha's context and a raw INSERT claiming Alpha's tenant while naming
    # BETA's client — the shape row security cannot catch, because the foreign-key
    # check that resolves client_id is documented to bypass it
    assert_isolated_role()
    das = ObligationType.objects.get(pk="DAS")

    with (
        tenant_context(alpha.tenant.id),
        pytest.raises(IntegrityError) as raised,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f"INSERT INTO {Obligation._meta.db_table} "  # noqa: S608
            "(id, tenant_id, client_id, obligation_type_id, competence_month, "
            "nominal_due_date, resolved_due_date, status, evidence_ref, "
            "created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '', now(), now())",
            [
                uuid6.uuid7(),
                alpha.tenant.id,
                beta.client.id,
                das.pk,
                date(2026, 7, 1),
                date(2026, 8, 20),
                date(2026, 8, 20),
                ObligationStatus.SCHEDULED,
            ],
        )

    # Then the COMPOSITE key rejects it by name. The single-column client_id foreign
    # key is satisfied by this row, so an error from any other constraint would mean
    # the hole is still open.
    assert "obligation_tenant_client_fk" in str(raised.value)


def test_the_uniqueness_that_makes_it_idempotent_is_a_database_constraint(
    alpha: Firm,
) -> None:
    # Given a generated calendar
    assert_isolated_role()
    das = ObligationType.objects.get(pk="DAS")
    with tenant_context(alpha.tenant.id):
        generate_das_calendar(alpha.client, starting_from=FROM_JULY)

        # When a duplicate is written directly, bypassing the generator entirely
        # Then the DATABASE refuses it. A generator that merely checked first would
        # still admit this, and two redelivered workers checking concurrently is
        # exactly the race acks_late makes routine.
        with pytest.raises(IntegrityError), transaction.atomic():
            Obligation.objects.create(
                tenant=alpha.tenant,
                client=alpha.client,
                obligation_type=das,
                competence_month=FROM_JULY,
                nominal_due_date=date(2026, 8, 20),
                resolved_due_date=date(2026, 8, 20),
            )
