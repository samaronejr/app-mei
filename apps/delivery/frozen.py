"""Adapt approved wire bytes to Django's existing SMTP backend without rerendering."""

import base64
from email.message import EmailMessage as MIMEMessage
from email.policy import Policy
from typing import override

from django.core.mail import EmailMessage

from apps.delivery.models import OutboundMessage


class FrozenMIME(MIMEMessage):
    """The backend's serialization port returns the already-approved artifact."""

    def __init__(self, wire: bytes) -> None:
        """Own the immutable bytes passed by the authorization phase."""
        super().__init__()
        self.wire = wire

    @override
    def as_bytes(
        self,
        unixfrom: bool = False,
        policy: Policy | None = None,
        *,
        linesep: str = "\r\n",
    ) -> bytes:
        """Keep the exact approved CRLF representation instead of serializing again."""
        return self.wire


class FrozenEmailMessage(EmailMessage):
    """One envelope recipient, potentially a deliberately shared approved To header."""

    def __init__(self, message: OutboundMessage, prepared: dict[str, str]) -> None:
        """Expose normal email metadata to redaction and the unchanged SMTP adapter."""
        super().__init__(
            subject=message.subject,
            body=prepared["body"],
            from_email=prepared["from"],
            to=message.recipients_json,
        )
        self.wire = base64.b64decode(prepared["frozen_mime"], validate=True)

    @override
    def message(self, *, policy: Policy | None = None) -> FrozenMIME:
        """Return the backend's byte source, not freshly generated headers."""
        return FrozenMIME(self.wire)
