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
    # dead-man's switch and the backup key with its twin: this endpoint became the only
    # thing that can notice either one has gone quiet, because neither a stopped
    # scheduler nor a failing nightly backup raises anything at all.
    #
    # `backup` reads "stale" here because a test database has never run a backup, and
    # 200/"ok" alongside it is the assertion that matters: a stale backup is an operator
    # task, never a reason to report the process unhealthy.
    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "stale",
    }
