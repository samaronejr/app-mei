"""Common inputs for native storage, writer, and fiscal reader witnesses."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection
from psycopg import sql
from psycopg.errors import Error as PostgresError

from apps.clients.models import Engagement
from apps.core.tenancy import tenant_context
from apps.cycles.records import ServiceCycle
from tests.integrations.capture_fixtures import MONTH

if TYPE_CHECKING:
    from datetime import date, datetime
    from uuid import UUID

    from django.db import IntegrityError

    from apps.cycles.fiscal_records import FiscalDocumentReview
    from tests.integrations.capture_fixtures import CaptureWorld


def other_cycle(world: CaptureWorld, *, start: date = MONTH) -> ServiceCycle:
    cycle = world.setup.cycle
    with tenant_context(world.tenant_id):
        engagement = Engagement.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            service_kind="documentos_mensal",
            starts_on=cycle.period_start,
            responsible=world.setup.graph.actor,
            provenance="staff_entry",
        )
        return ServiceCycle.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            engagement=engagement,
            playbook_id=cycle.playbook_id,
            playbook_version_id=cycle.playbook_version_id,
            tax_profile_id=cycle.tax_profile_id,
            period_start=start,
            period_end=start.replace(day=28),
        )


def row_image(review_id: UUID) -> bytes:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.record_send(r) FROM cycles_fiscaldocumentreview AS r "
            "WHERE r.id = %s",
            [review_id],
        )
        row = cursor.fetchone()
        assert row is not None
        return bytes(row[0])


def constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    assert isinstance(cause, PostgresError)
    return cause.diag.constraint_name


def blank_review_notes() -> list[str]:
    # Unicode White_Space plus the four legacy information separators.
    points = (
        *range(0x09, 0x0E),
        *range(0x1C, 0x21),
        0x85,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    )
    spaces = [chr(point) for point in points]
    return ["", *spaces, "".join(spaces)]


def insert_review_rows(
    review: FiscalDocumentReview, edges: list[tuple[UUID, UUID | None]]
) -> None:
    """Use one native statement without ORM or service graph checks."""
    params: list[UUID | str | datetime | None] = []
    for key, predecessor in edges:
        params.extend(
            (
                key,
                review.tenant_id,
                review.client_id,
                review.document_id,
                review.revision_id,
                review.cycle_id,
                review.reviewer_id,
                predecessor,
                review.note,
                review.input_digest,
                review.reviewed_at,
            )
        )
    row = sql.SQL("(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'aceito',1)")
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "INSERT INTO cycles_fiscaldocumentreview "
                "(id, tenant_id, client_id, document_id, revision_id, cycle_id, "
                "reviewer_id, supersedes_id, note, input_digest, reviewed_at, "
                "decision, version) VALUES {}"
            ).format(sql.SQL(", ").join([row] * len(edges))),
            params,
        )


def insert_review_edge(review: FiscalDocumentReview, predecessor: UUID | None) -> UUID:
    key = uuid4()
    insert_review_rows(review, [(key, predecessor)])
    return key
