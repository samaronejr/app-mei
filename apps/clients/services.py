"""The sanctioned write paths for portfolio changes.

Assignment is what the `limited` permission level resolves against, so adding or
removing one silently changes who can read a client's tax and revenue data. Every
change therefore leaves an `audit.Event` naming the actor, the client and the account
whose reach moved.
"""

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    OnboardingItem,
    OnboardingItemTemplate,
)

OBJECT_TYPE = "clients.ClientAssignment"


def assign_client(
    *,
    actor: User,
    client: ClientCompany,
    user: User,
    role: str = AssignmentRole.PRIMARY,
) -> ClientAssignment:
    """Put an account on a client, recording who did it."""
    assignment = ClientAssignment(
        tenant_id=client.tenant_id,
        client=client,
        user=user,
        role=role,
    )
    assignment.save()
    _record(actor=actor, assignment=assignment, change="assigned")
    return assignment


def unassign_client(*, actor: User, assignment: ClientAssignment) -> None:
    """Take an account off a client, recording who did it.

    The event is written **before** the delete so the row's identifiers are still
    readable, and both land in the same request transaction — a rollback discards
    the pair together rather than leaving a record of a removal that did not happen.
    """
    _record(actor=actor, assignment=assignment, change="unassigned")
    assignment.delete()


def _record(*, actor: User, assignment: ClientAssignment, change: str) -> None:
    record_event(
        action=AuditAction.ASSIGNMENT_CHANGED,
        tenant_id=assignment.tenant_id,
        actor=actor,
        obj=ObjectRef(type=OBJECT_TYPE, id=str(assignment.pk)),
        # References only. The audit trail is append-only and LGPD grants erasure,
        # so a client's name or CNPJ written here would be unerasable personal data.
        metadata={
            "change": change,
            "client_id": str(assignment.client_id),
            "user_id": str(assignment.user_id),
            "role": assignment.role,
        },
    )


def create_default_checklist(client: ClientCompany) -> list[OnboardingItem]:
    """Give a newly registered client the standard checklist.

    Driven entirely by the template table, so a Phase 3 connector adds its own item
    by inserting a row rather than by shipping a migration and a code change.

    The key and label are copied onto each item rather than referenced: a template
    row may later be retired or reworded, and a client's checklist must keep saying
    what was actually asked of them at the time.
    """
    templates = OnboardingItemTemplate.objects.filter(is_active=True)
    return OnboardingItem.objects.bulk_create(
        [
            OnboardingItem(
                tenant_id=client.tenant_id,
                client=client,
                key=template.key,
                label=template.label,
                position=template.position,
            )
            for template in templates
        ],
    )


__all__ = ["assign_client", "create_default_checklist", "unassign_client"]
