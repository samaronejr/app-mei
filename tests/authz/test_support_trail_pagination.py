"""Every owner-visible grant event stays reachable without per-row queries."""

import re
from http import HTTPStatus

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.audit.models import AuditAction, Event
from apps.core.tenancy import tenant_context
from tests.authz.test_support_grant import _grant_for, _session_for, _staff
from tests.tenants.test_cancellation_retention import enabled_firm

pytestmark = pytest.mark.django_db(transaction=True)
HISTORIC_ROWS = 205


@pytest.fixture
def trail() -> tuple[Client, str]:
    firm = enabled_firm("support-trail-pages")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    with tenant_context(firm.tenant.pk):
        Event.objects.bulk_create(
            [
                Event(
                    tenant=firm.tenant,
                    actor=staff,
                    action=AuditAction.SUPPORT_REQUEST,
                    object_type="SupportGrant",
                    object_id=str(grant.pk),
                    metadata={"method": "GET", "path": f"/historic/{index}/"},
                )
                for index in range(HISTORIC_ROWS)
            ]
        )
    owner = _session_for(firm.owner, firm.host)
    return owner, f"/configuracoes/suporte/{grant.pk}/trilha/"


def _history_ids(html: str) -> set[int]:
    return {int(value) for value in re.findall(r"/historic/(\d+)/", html)}


def test_owner_can_follow_every_page_of_the_grant_trail(
    trail: tuple[Client, str],
) -> None:
    # Given more than two pages of requests and their lifecycle events.
    owner, path = trail
    first = owner.get(path)
    assert first.status_code == HTTPStatus.OK
    html = first.content.decode()

    # When the owner follows the actual next-page links.
    seen = _history_ids(html)
    for number in (2, 3):
        destination = f"?pagina={number}"
        assert f'href="{destination}"' in html
        response = owner.get(path + destination)
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        seen.update(_history_ids(html))

    # Then all older rows are reachable, and the last page links back only.
    assert seen == set(range(HISTORIC_ROWS))
    assert 'href="?pagina=2"' in html
    assert 'href="?pagina=4"' not in html


def test_trail_queries_stay_flat_for_full_and_short_pages(
    trail: tuple[Client, str],
) -> None:
    # Given the same owner session and a 100-row first page.
    owner, path = trail
    owner.get("/configuracoes/")

    # When full and short pages render, including every actor's email.
    with CaptureQueriesContext(connection) as full_queries:
        first = owner.get(path + "?pagina=1")
    with CaptureQueriesContext(connection) as short_queries:
        last = owner.get(path + "?pagina=3")

    # Then the final five historical rows render without a row-dependent query cost.
    assert first.status_code == last.status_code == HTTPStatus.OK
    assert len(_history_ids(first.content.decode())) == 100
    assert _history_ids(last.content.decode()) == set(range(5))
    assert len(full_queries) == len(short_queries), (
        len(full_queries),
        len(short_queries),
    )
