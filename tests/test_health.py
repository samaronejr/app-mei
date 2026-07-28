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

    # Then it answers 200 with a minimal JSON body. The scheduler key arrived with
    # the dead-man's switch: this endpoint became the only thing that can notice beat
    # has gone quiet, because a stopped scheduler raises nothing at all.
    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == {"status": "ok", "scheduler": "alive"}
