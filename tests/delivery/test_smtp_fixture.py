"""The loopback SMTP peer must not depend on hostname resolution for its greeting."""

import smtplib
import socket

import pytest

from tests.delivery.smtp import FakeSMTP


def test_peer_accepts_messages_without_hostname_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[str] = []

    def observe_dns(name: str = "") -> str:
        lookups.append(name)
        return "resolver.fixture.test"

    monkeypatch.setattr(socket, "getfqdn", observe_dns)
    message = "Subject: loopback probe\r\n\r\nSynthetic payload.\r\n"

    with (
        FakeSMTP() as peer,
        smtplib.SMTP(
            "127.0.0.1", peer.port, local_hostname="fixture.test", timeout=3
        ) as client,
    ):
        assert (
            client.sendmail("sender@fixture.test", ["recipient@fixture.test"], message)
            == {}
        )

    assert peer.received == [message.encode("ascii")]
    assert lookups == []
    assert peer.stopped.is_set()
    assert peer.thread is not None
    assert not peer.thread.is_alive()
