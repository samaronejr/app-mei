"""Wiring that makes a new client arrive with its checklist already attached.

A signal rather than an override of `ClientCompany.save`: the checklist is a
consequence of registering a client, not part of what a client *is*, and a firm that
later imports clients in bulk gets the same behaviour without remembering to call
anything.
"""

from typing import Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.clients.models import ClientCompany
from apps.clients.services import create_default_checklist


@receiver(post_save, sender=ClientCompany, dispatch_uid="clients.default_checklist")
def attach_default_checklist(
    # Django fixes the receiver signature, so sender and kwargs are accepted and
    # unused. Same rationale as the ARG002 allowance migrations already carry.
    sender: object,  # noqa: ARG001
    instance: ClientCompany,
    created: bool,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Create the standard checklist the first time a client is saved."""
    if not created:
        return
    create_default_checklist(instance)
