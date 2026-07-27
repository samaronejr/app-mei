"""The /healthz probe must answer 200 with a JSON body."""

from http import HTTPStatus

from django.test import Client
from django.urls import reverse


def test_healthz_returns_200_json(client: Client) -> None:
    # Given a running application and no authentication
    url = reverse("healthz")

    # When the health probe is called
    response = client.get(url)

    # Then it answers 200 with a minimal JSON body
    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == {"status": "ok"}
