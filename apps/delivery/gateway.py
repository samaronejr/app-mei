"""The only outbox egress: one message, one recipient, no open transaction."""

import smtplib
from collections.abc import Callable

from django.core.mail import EmailMessage

from apps.core.tasks import assert_no_transaction
from apps.delivery.contracts import Claim, Reply
from apps.delivery.frozen import FrozenEmailMessage
from apps.delivery.models import MessageState

SMTP_TEMPORARY_MIN = 400
SMTP_PERMANENT_MIN = 500


def smtp_send(email: EmailMessage) -> Reply:
    """Only the final DATA reply is acceptance; RCPT alone proves nothing."""
    assert_no_transaction()
    try:
        count = email.send(fail_silently=False)
    except smtplib.SMTPRecipientsRefused:
        return Reply(MessageState.REJECTED, "smtp_recipients_refused")
    except smtplib.SMTPResponseException as error:
        return Reply(
            MessageState.TEMPORARY
            if SMTP_TEMPORARY_MIN <= error.smtp_code < SMTP_PERMANENT_MIN
            else MessageState.REJECTED,
            f"smtp_{error.smtp_code}",
        )
    except ConnectionRefusedError:
        return Reply(MessageState.TEMPORARY, "smtp_unavailable")
    except OSError:
        # Conservatively unknown even when the library cannot tell us whether DATA
        # reached the peer. Never turn an ambiguous disconnect into a blind resend.
        return Reply(MessageState.UNKNOWN, "smtp_connection_lost")
    return Reply(
        MessageState.ACCEPTED if count == 1 else MessageState.TEMPORARY,
        "" if count == 1 else "smtp_no_acceptance",
    )


def send(claim: Claim, *, before_network_io: Callable[[], None] | None = None) -> Reply:
    """Authorize once, journal intent durably, send, then journal the reply."""
    from apps.delivery import journal  # noqa: PLC0415 - phased service dependency
    from apps.delivery.policy import content_digest, render_body  # noqa: PLC0415
    from apps.delivery.privacy import delivery_secrets  # noqa: PLC0415
    from apps.delivery.services import authorize_and_prepare  # noqa: PLC0415

    assert_no_transaction()
    if before_network_io is not None:
        before_network_io()
    message, secrets = authorize_and_prepare(claim)
    if message.state != MessageState.SENDING:
        return Reply(message.state, message.last_error_code)
    public_body = render_body(message)
    if content_digest(message, rendered_body=public_body) != message.content_digest:
        return Reply(MessageState.CANCELLED, "content_changed")
    email = (
        FrozenEmailMessage(message, secrets)
        if "frozen_mime" in secrets
        else EmailMessage(
            subject=message.subject,
            body=render_body(message, secrets) if secrets else public_body,
            to=message.recipients_json,
            headers={"Message-ID": f"<{message.pk}@outbox.local>"},
        )
    )
    with delivery_secrets(
        str(email.subject),
        str(email.body),
        *message.recipients_json,
        *secrets.values(),
    ):
        journal.append(message, "intent_started")
        reply = smtp_send(email)
        journal.append(message, reply.state, code=reply.code)
        return reply
