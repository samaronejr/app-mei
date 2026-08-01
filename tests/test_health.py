"""The /healthz probe must answer 200 with a JSON body."""

from http import HTTPStatus

import pytest
from django.test import Client
from django.urls import reverse


@pytest.mark.django_db
def test_healthz_returns_200_json(client: Client) -> None:
    # Given a running application, a scheduler that has checked in, and no
    # authentication
    url = reverse("healthz")

    # When the health probe is called
    response = client.get(url)

    # Then it answers 200 with a minimal JSON body. The scheduler key arrived with the
    # dead-man's switch, the backup key with its twin, and the disk key with the WAL
    # fuse: this endpoint became the only thing that can notice any of them has gone
    # quiet, because a stopped scheduler, a failing nightly backup and a filling disk
    # all raise exactly nothing.
    #
    # `backup` reads "stale" and `disk` "unknown" here because a test database has never
    # run a backup and so has never measured anything. 200/"ok" alongside them is the
    # assertion that matters: neither is a reason to report the process unhealthy.
    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "stale",
        "disk": "unknown",
    }
