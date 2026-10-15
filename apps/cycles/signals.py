"""The flag-grant hook: enabling `cycles_r1` installs the system packs.

`TenantAdmin` is the documented place a flag reaches a firm, and its `save_model`
is an ordinary save — so `post_save` on `Tenant` is the one choke point every
grant path (admin, a management command, a test) passes through. The pack rows
land inside the same transaction as the flag itself: a firm either has both the
entitlement and the content, or neither.
"""

from typing import Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.tenants.models import Tenant


@receiver(post_save, sender=Tenant, dispatch_uid="cycles.install_system_packs")
def install_packs_on_flag(
    # Django fixes the receiver signature, so sender and kwargs are accepted and
    # unused. Same rationale as the ARG002 allowance migrations already carry.
    sender: object,  # noqa: ARG001
    instance: Tenant,
    raw: bool = False,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Run the pack installer whenever a tenant row is saved.

    The installer itself is the gate — `tenant_has` answers False for a firm
    without the flag and nothing is written — so every save of every tenant is
    safe to pass through it. `raw` means a fixture load, not an entitlement
    change, and is the one save this hook deliberately ignores.
    """
    if raw:
        return
    # Local import: the installer reads models that must exist, and this module
    # loads while migrations may still be runnable against a schema without them.
    from apps.cycles.packs import install_system_packs  # noqa: PLC0415

    install_system_packs(instance)
