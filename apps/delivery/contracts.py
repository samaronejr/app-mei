"""Value-only boundaries between claim, SMTP egress and finalization."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Claim:
    """A committed lease, not authority until the gateway revalidates it."""

    tenant_id: UUID
    message_id: UUID
    fence: int
    owner: UUID


@dataclass(frozen=True, slots=True)
class Reply:
    """Minimized SMTP outcome; never retain exception content."""

    state: str
    code: str = ""
