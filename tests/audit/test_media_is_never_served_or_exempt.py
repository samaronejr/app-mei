"""Nothing serves `/media/`, and nothing exempts it from the Marco Civil access log.

Two independent facts that only mean something together.

The exemption list used to carry `MEDIA_URL`. For a fiscal document vault that would
have excluded the access log's most important rows by construction — who fetched which
client's evidence, and when — and it would have done so silently, before the vault
existed for anyone to notice the absence.

Removing it is necessary and not sufficient. If some future line served `/media/`
directly (`static(settings.MEDIA_URL, ...)`, or a Caddy `file_server`), every document
would be world-readable with no session, tenant, client or policy involved, and the log
entry for a request that should never have been possible is not a consolation. So the
route is asserted absent as well.
"""

from http import HTTPStatus

import pytest
from django.conf import settings
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db

FIRM_HOST = "acme.localhost"
PORTAL_HOST = "acme-portal.localhost"
PROBE = "probe-does-not-exist.pdf"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def test_the_media_prefix_is_not_exempt_from_the_access_log() -> None:
    # Given the exemption list
    exempt = settings.ACCESS_LOG_EXEMPT_PREFIXES

    # Then the media prefix is absent, so a download cannot be skipped by prefix.
    # /healthz and the static prefix stay: infrastructure traffic, not a person
    # reaching personal data.
    assert settings.MEDIA_URL not in exempt
    assert "/healthz" in exempt
    assert settings.STATIC_URL in exempt


@pytest.mark.parametrize("host", [FIRM_HOST, PORTAL_HOST])
def test_nothing_serves_the_media_prefix_on_either_host(host: str) -> None:
    # Given a request for a media path on each host the product answers
    response = Client().get(f"{settings.MEDIA_URL}{PROBE}", headers={"host": host})

    # Then nothing is there. One added line would make every stored document readable
    # with no session, tenant, client or policy involved, and an access-log row for a
    # request that should have been impossible is not a mitigation.
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        f"{host} served {settings.MEDIA_URL}{PROBE} with {response.status_code}; "
        f"the vault view must be the only route to a document's bytes"
    )


def test_the_exemption_list_is_matched_by_prefix_so_a_longer_path_is_covered() -> None:
    # Given how the middleware decides -- startswith over the tuple
    exempt = tuple(settings.ACCESS_LOG_EXEMPT_PREFIXES)

    # Then a nested media path is not exempt either. Checking equality rather than
    # prefix would have left /media/anything/at/all covered while /media/ was not, and
    # the difference is invisible until someone reads for a document that was fetched.
    assert not f"{settings.MEDIA_URL}{PROBE}".startswith(exempt)
    assert f"{settings.STATIC_URL}css/app.css".startswith(exempt)
