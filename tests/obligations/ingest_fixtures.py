"""Shared real-role controls for AXRM-012, not mocked visibility checks."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from django.db import connection
from django.db.backends.utils import CursorWrapper
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import client_context, tenant_context
from apps.cycles.intake import tenant_quarantine
from apps.tenants.models import Tenant
from tests.portal.request_fixtures import PortalSetup, portal_setup

PDF = b"%PDF-1.4\n% synthetic SITFIS report\n%%EOF\n"


def scoped_upload[R, **P](
    scope: Tenant, service: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """Use the real preflight, out-of-transaction PUT, then scoped INSERT."""
    return tenant_quarantine(scope.pk, partial(service, *args, **kwargs))


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
