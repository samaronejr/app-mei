"""Shared real-role controls for AXRM-012, not mocked visibility checks."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.db.backends.utils import CursorWrapper
from pytest_django.fixtures import SettingsWrapper

from tests.portal.request_fixtures import (
    PortalSetup,
    portal_setup,
    scoped_portal_cursor,
)

PDF = b"%PDF-1.4\n% synthetic SITFIS report\n%%EOF\n"


def ingest_setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = False
    return portal_setup("visibility")


@contextmanager
def portal_cursor(setup: PortalSetup) -> Iterator[CursorWrapper]:
    """Use the real NOLOGIN portal role, proving its flags before every assertion."""
    with scoped_portal_cursor(setup.graph.tenant.pk, setup.graph.client.pk) as cursor:
        yield cursor
