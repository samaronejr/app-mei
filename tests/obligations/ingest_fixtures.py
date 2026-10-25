"""Shared real-role controls for AXRM-012, not mocked visibility checks."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.db import connection
from django.db.backends.utils import CursorWrapper
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import client_context, tenant_context
from tests.portal.request_fixtures import PortalSetup, portal_setup

PDF = b"%PDF-1.4\n% synthetic SITFIS report\n%%EOF\n"


def ingest_setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = False
    return portal_setup("visibility")


@contextmanager
def portal_cursor(setup: PortalSetup) -> Iterator[CursorWrapper]:
    """Use the real NOLOGIN portal role, proving its flags before every assertion."""
    with (
        tenant_context(setup.graph.tenant.pk),
        client_context(setup.graph.client.pk),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        assert cursor.fetchone() == ("app_portal", False, False)
        yield cursor
