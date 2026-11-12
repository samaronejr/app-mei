"""Closed Focus operation registration with live authority and budget policies.

FC-EMP is the operator's configuration channel (A6: included in the plan, journaled,
never from the web). FC-NFE/FC-NFS are the capture reads todo 21 schedules; their
admission needs the client's capture authorization and a usable provider-held
certificate on the exact link (`authority_for`). FC-CIE (todo 22) and FC-PDF (staff
view, billing still UNKNOWN in A6) register with the todos that use them.
"""

from typing import Final, Literal

from apps.integrations.authority import authority_for
from apps.integrations.budget import BudgetPolicy
from apps.integrations.call_contracts import Operation, register_operation
from apps.integrations.call_models import ProviderCall

CATALOG: Final[tuple[tuple[str, Literal["configuration", "read"]], ...]] = (
    ("FC-EMP", "configuration"),
    ("FC-NFE", "read"),
    ("FC-NFS", "read"),
)


def focus_authority(call: ProviderCall) -> bool:
    """Recheck the client's authority for this operation on this exact link."""
    return authority_for(call.client, call.operation, link=call.link).usable


def register_focus_operations() -> None:
    """Register the Focus operations; nothing here can issue or register an event."""
    for code, kind in CATALOG:
        register_operation(
            Operation(
                code,
                "focus",
                "v1",
                kind,
                "document_capture",
                "incluido_no_plano",
                BudgetPolicy(focus_authority),
            )
        )
