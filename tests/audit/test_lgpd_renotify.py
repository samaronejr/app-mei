"""Manual redelivery of pending LGPD notifications."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.audit.models import (
    DataSubjectRelationship,
    DataSubjectRequest,
    DataSubjectRequestType,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _request(**overrides: object) -> DataSubjectRequest:
    values = {
        "requester_name": "Maria Souza",
        "cpf": "529.982.247-25",
        "email": "maria@exemplo.example",
        "relationship": DataSubjectRelationship.HOLDER,
        "request_type": DataSubjectRequestType.DELETION,
        "detail": "Solicito a exclusão dos meus dados.",
        **overrides,
    }
    return DataSubjectRequest.objects.create(**values)


def test_renotify_sends_only_pending_rows_and_stamps_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one pending request and one whose notification already succeeded
    stamped_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    pending = _request()
    already_notified = _request(encarregado_notified_at=stamped_at)
    send = Mock()
    retry_at = datetime(2026, 8, 10, 13, tzinfo=UTC)
    monkeypatch.setattr("apps.lgpd.views._notify_encarregado", send)
    monkeypatch.setattr(timezone, "now", Mock(return_value=retry_at))

    # When an operator runs the manual retry command
    call_command("lgpd_renotify", verbosity=0)

    # Then only the pending row is sent and marked complete
    pending.refresh_from_db()
    already_notified.refresh_from_db()
    send.assert_called_once_with(pending)
    assert pending.encarregado_notified_at == retry_at
    assert already_notified.encarregado_notified_at == stamped_at


def test_renotify_records_failure_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a pending request while SMTP remains unavailable
    pending = _request()
    error = RuntimeError("SMTP ainda indisponível")
    send = Mock(side_effect=error)
    monkeypatch.setattr("apps.lgpd.views._notify_encarregado", send)

    # When an operator attempts manual redelivery
    with pytest.raises(CommandError, match=r"1 LGPD notification.*failed"):
        call_command("lgpd_renotify", verbosity=0)

    # Then the durable status remains retryable and records only the exception text
    pending.refresh_from_db()
    assert pending.encarregado_notified_at is None
    assert pending.notification_last_error == str(error)
    send.assert_called_once_with(pending)


def test_renotify_bounds_each_operator_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given more pending requests than the requested batch size
    _request(email="first@exemplo.example")
    _request(email="second@exemplo.example")
    send = Mock()
    monkeypatch.setattr("apps.lgpd.views._notify_encarregado", send)

    # When the operator limits this run to one request
    call_command("lgpd_renotify", batch_size=1, verbosity=0)

    # Then exactly one delivery is attempted and the other remains retryable
    send.assert_called_once()
    assert (
        DataSubjectRequest.objects.filter(
            encarregado_notified_at__isnull=False,
        ).count()
        == 1
    )
    assert (
        DataSubjectRequest.objects.filter(
            encarregado_notified_at__isnull=True,
        ).count()
        == 1
    )


def test_renotify_is_idempotent_after_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one pending request
    pending = _request()
    send = Mock()
    monkeypatch.setattr("apps.lgpd.views._notify_encarregado", send)

    # When the operator runs the same command twice
    call_command("lgpd_renotify", verbosity=0)
    first_stamp = DataSubjectRequest.objects.values_list(
        "encarregado_notified_at",
        flat=True,
    ).get(pk=pending.pk)
    call_command("lgpd_renotify", verbosity=0)

    # Then the delivered request is neither sent nor stamped a second time
    pending.refresh_from_db()
    send.assert_called_once_with(pending)
    assert pending.encarregado_notified_at == first_stamp
