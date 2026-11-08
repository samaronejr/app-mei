"""016 owns the explicit test admission needed by its new transport contract."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.integrations.call_dispatch import _wire_window
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import EnvioNaoAutorizado
from tests.fakes.provider import Reply, permitted_send
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_transport import serpro_request

pytest_plugins = ("tests.integrations.sitfis_transport_fixture",)


def test_synthetic_sitfis_transport_has_explicit_test_admission() -> None:
    pdf = b"%PDF-synthetic-transport"
    with FakeSerpro((Reply(body=pdf),)) as peer:
        response = permitted_send(
            ProviderTransport("serpro", "fake", peer.origin),
            "SP-SIT-R",
            serpro_request("RELATORIOSITFIS92", "SITFIS"),
        )
        assert response.body == pdf
        assert [request.path for request in peer.requests] == ["/Emitir"]


def test_synthetic_fixture_does_not_admit_an_unguarded_production_window() -> None:
    with FakeSerpro((Reply(),)) as peer:
        client = ProviderTransport("serpro", "fake", peer.origin)
        with (
            _wire_window(
                "serpro", "fake", "SP-SIT-R", timezone.now() + timedelta(hours=1)
            ),
            pytest.raises(EnvioNaoAutorizado),
        ):
            client.send("SP-SIT-R", serpro_request("RELATORIOSITFIS92", "SITFIS"))
        assert peer.requests == []
