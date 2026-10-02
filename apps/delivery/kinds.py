"""T11 message kinds and their pre-send re-evaluation hooks (S7.6, S15 I).

Each hook runs in the gateway's authorization transaction, under the message lock,
immediately before network I/O (`apps.delivery.services.authorize_and_prepare`), and
reads the live domain rows rather than anything captured at enqueue time. A refusal
returns a reason code; the message is cancelled with it and nothing is sent.

* `portal_invite` -- cancelled when the invitation was revoked, accepted or expired,
  or (through the shared `still_valid`) when the contact became inactive or lost its
  e-mail permission. `prepare` mints the one-time token for this send only. When
  the provider refuses it (T14), `on_rejected` withdraws the invitation nobody
  received and opens an exception on the client's newest open cycle.
* `request_notice` -- cancelled when the cycle no longer has any request waiting on
  the client. A notice to a contact without a seat embeds that contact's live
  invitation, re-minted at dispatch; if the invitation meanwhile became unusable the
  notice still goes, with the portal link alone.
* `request_reminder` -- cancelled when the request was answered in the meantime
  (`request_submitted`: the client uploaded against it) or left the client's hands
  (`request_closed`: waived, accepted, replaced).
"""

from typing import Final

from django.utils import timezone

from apps.clients.maintenance import withdraw_undelivered_invite
from apps.cycles.evidence import CLIENT_OPEN_STATES, EvidenceRequest, counted_links
from apps.cycles.models import CycleState, ServiceCycle
from apps.cycles.services.exceptions import open_exception
from apps.delivery.models import OutboundMessage
from apps.delivery.policy import MessageKind, register_kind

INVITE_REJECTED: Final = "convite_recusado"


def _invite_problem(message: OutboundMessage) -> str:
    invite = message.invite
    if invite is None:
        return "invite_missing"
    if invite.accepted_at is not None:
        return "invite_accepted"
    if invite.revoked_at is not None:
        return "invite_revoked"
    if invite.expires_at <= timezone.now():
        return "invite_expired"
    if invite.email.lower() != message.recipients_json[0].lower():
        return "recipient_changed"
    return ""


def _invite_valid(message: OutboundMessage) -> tuple[bool, str]:
    problem = _invite_problem(message)
    return (not problem, problem)


def _accept_url(message: OutboundMessage) -> dict[str, str]:
    from apps.accounts.invites import mint_token_for_dispatch  # noqa: PLC0415
    from apps.accounts.views import portal_accept_link  # noqa: PLC0415

    if message.invite is None:
        reason = "invite_missing"
        raise ValueError(reason)
    raw_token = mint_token_for_dispatch(message.invite)
    return {"accept_url": portal_accept_link(message.invite.tenant.slug, raw_token)}


def _withdraw_rejected_invite(message: OutboundMessage) -> None:
    """Stand down the refused invitation, then give the problem an owner.

    The message has no cycle of its own, so the exception lands on the client's
    newest open cycle; a client with none keeps the withdrawal and its audit event.
    """
    if message.invite_id is None:
        return
    withdraw_undelivered_invite(
        invite_id=message.invite_id,
        requested_by=message.created_by,
        reason=message.last_error_code or message.state,
    )
    cycle = (
        ServiceCycle.objects.select_for_update()
        .filter(client_id=message.client_id)
        .exclude(
            state__in=[
                CycleState.COMPLETED,
                CycleState.WITH_PENDING,
                CycleState.CANCELLED,
            ]
        )
        .order_by("-period_start", "-created_at", "pk")
        .first()
    )
    if cycle is None:
        return
    open_exception(
        INVITE_REJECTED,
        message.invite_id,
        actor=None,
        cycle_id=cycle.pk,
        expected_version=cycle.version,
        owner=cycle.owner or message.created_by,
        review_on=timezone.localdate(),
        evidence_refs=(message.pk,),
    )


def _embedded_invite(message: OutboundMessage) -> dict[str, str]:
    """Embed a fresh invitation link when the recipient still has no seat."""
    if message.invite is None or _invite_problem(message):
        return {}
    return _accept_url(message)


def _cycle_waiting(message: OutboundMessage) -> tuple[bool, str]:
    waiting = (
        EvidenceRequest.objects.filter(
            cycle_id=message.cycle_id, state__in=CLIENT_OPEN_STATES
        )
        .exclude(links__isnull=False)
        .exists()
    )
    return (waiting, "" if waiting else "requests_answered")


def _request_waiting(message: OutboundMessage) -> tuple[bool, str]:
    request = message.evidence_request
    if request is None:
        return False, "request_missing"
    current = EvidenceRequest.objects.filter(pk=request.pk).first()
    if current is None or current.state not in CLIENT_OPEN_STATES:
        return False, "request_closed"
    if counted_links().filter(request_id=request.pk).exists():
        return False, "request_submitted"
    return True, ""


def register() -> None:
    """Register the three kinds; called once from `DeliveryConfig.ready`."""
    register_kind(
        "portal_invite",
        MessageKind(
            "Convite para o portal do cliente",
            "delivery/email/portal_invite.txt",
            _invite_valid,
            _accept_url,
            _withdraw_rejected_invite,
        ),
    )
    register_kind(
        "request_notice",
        MessageKind(
            "Seu escritório pediu documentos",
            "delivery/email/request_notice.txt",
            _cycle_waiting,
            _embedded_invite,
        ),
    )
    register_kind(
        "request_reminder",
        MessageKind(
            "Lembrete: documento pendente",
            "delivery/email/request_reminder.txt",
            _request_waiting,
            _embedded_invite,
        ),
    )
