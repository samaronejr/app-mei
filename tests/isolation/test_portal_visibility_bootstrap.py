"""Reapplying production bootstrap must not restore the old client-only policies."""

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.db import ProgrammingError, connection, transaction
from psycopg import sql
from pytest_django.fixtures import SettingsWrapper

from apps.core.identifiers import uuid7
from tests.isolation.test_portal_visibility_predicate import _document, _obligation
from tests.obligations.ingest_fixtures import ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup

if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.django_db(transaction=True)


def _bootstrap_sql() -> str:
    source = (Path(settings.BASE_DIR) / "ops/sql/roles.sql").read_text()
    # This is executable SQL, not a prose pin or an oracle derived from the migration.
    match = re.search(
        r"DO \$\$\s+DECLARE\s+visible_table text;.*?\n\$\$;",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group()


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


@pytest.mark.parametrize("table", ["obligations_document", "obligations_obligation"])
@pytest.mark.parametrize("side", ["select", "insert"])
def test_bootstrap_reasserts_both_portal_visibility_predicates(
    setup: PortalSetup, table: str, side: str
) -> None:
    # Given actual same-client rows and a drifted client-only policy.
    factory = _document if table == "obligations_document" else _obligation
    hidden = factory(setup, visible=False)
    visible = factory(setup, visible=True)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(
            sql.SQL(
                "ALTER POLICY {} ON {} "
                "USING (client_id = NULLIF(current_setting("
                "'app.client_id', true), '')::uuid) "
                "WITH CHECK (client_id = NULLIF(current_setting("
                "'app.client_id', true), '')::uuid)"
            ).format(
                sql.Identifier(f"{table}_portal_client_isolation"),
                sql.Identifier(table),
            )
        )
        # When the actual upgraded-schema block in roles.sql is reapplied.
        cursor.execute(_bootstrap_sql())
        if side == "select":
            with portal_cursor(setup) as portal:
                portal.execute(
                    sql.SQL("SELECT id FROM {} WHERE id IN (%s, %s)").format(
                        sql.Identifier(table)
                    ),
                    [hidden.pk, visible.pk],
                )
                # Then the real portal role sees only the visible row.
                assert portal.fetchall() == [(visible.pk,)]
        else:
            cursor.execute(
                "ALTER TABLE obligations_document "
                "DISABLE TRIGGER document_portal_client_upload"
            )
            cursor.execute("GRANT INSERT ON obligations_obligation TO app_portal")
            # No RETURNING: its SELECT policy would mask a broken WITH CHECK.
            if table == "obligations_document":
                statement = (
                    "INSERT INTO obligations_document "
                    "(id, tenant_id, client_id, storage_key, sha256, "
                    "original_filename, content_type, byte_size, uploaded_by_id, "
                    "created_at, portal_visible) VALUES (%s, %s, %s, "
                    "'bootstrap-insert-control', %s, 'x.pdf', 'application/pdf', "
                    "1, %s, now(), false)"
                )
                params: list[UUID | str] = [
                    uuid7(),
                    setup.graph.tenant.pk,
                    setup.graph.client.pk,
                    "c" * 64,
                    setup.portal_user.pk,
                ]
            else:
                statement = (
                    "INSERT INTO obligations_obligation "
                    "(id, tenant_id, client_id, obligation_type_id, competence_month, "
                    "nominal_due_date, resolved_due_date, status, evidence_ref, "
                    "created_at, updated_at, portal_visible) VALUES (%s, %s, %s, "
                    "'DAS', '2030-08-01', '2030-09-20', '2030-09-20', "
                    "'scheduled', '', now(), now(), false)"
                )
                params = [uuid7(), setup.graph.tenant.pk, setup.graph.client.pk]
            with (
                pytest.raises(ProgrammingError, match="row-level security policy"),
                transaction.atomic(),
                portal_cursor(setup) as portal,
            ):
                portal.execute(statement, params)
        # Both the drift and the temporary write grant disappear even on failure.
        transaction.set_rollback(True)
