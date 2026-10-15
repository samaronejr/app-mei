"""Freeze the transport artifact once; neither approval nor dispatch renders it."""

import base64
import hashlib
from datetime import UTC
from email.generator import BytesGenerator
from email.utils import format_datetime
from io import BytesIO

from django.conf import settings
from django.core.mail import EmailMessage

from apps.cycles.effects.manifest import JSONValue, canonicalize
from apps.cycles.effects.records import ActionProposal


def smtp_account() -> str:
    """Bind the configured SMTP destination without storing its credentials."""
    identity: list[JSONValue] = [
        settings.EMAIL_HOST,
        str(settings.EMAIL_PORT),
        settings.EMAIL_HOST_USER,
        settings.DEFAULT_FROM_EMAIL,
    ]
    return hashlib.sha256(canonicalize(identity)).hexdigest()


def freeze_mail(proposal: ActionProposal, email: EmailMessage) -> dict[str, JSONValue]:
    """Use persisted identity/time and preserve exactly the CRLF MIME bytes."""
    email.extra_headers.update(
        {
            "Date": format_datetime(proposal.created_at.astimezone(UTC), usegmt=True),
            "Message-ID": f"<{proposal.pk}@outbox.local>",
        }
    )
    message = email.message()
    buffer = BytesIO()
    BytesGenerator(buffer, mangle_from_=False).flatten(message, linesep="\r\n")
    wire = buffer.getvalue()
    # SMTP DATA terminates the final content line with CRLF. Bind that byte
    # sequence now, not a shorter MIME value that the adapter silently extends.
    if not wire.endswith(b"\r\n"):
        wire += b"\r\n"
    return {
        "from": str(email.from_email),
        "headers": {key: str(value) for key, value in message.items()},
        "mime_base64": base64.b64encode(wire).decode("ascii"),
        "mime_sha256": hashlib.sha256(wire).hexdigest(),
    }
