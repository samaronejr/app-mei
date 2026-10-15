"""Object loading that makes 404, not 403, the answer to an invisible client.

S8.1's permissions model is two-layered: the capability matrix decides WHAT an
account may do, and the portfolio decides WHICH clients it may do it to. A view
that gates on `cycles.operate` and then loads a client by primary key has only
half of that contract — `ClientCompany.objects.get` is tenant-scoped, but a staff
accountant holds `cycles.operate` at FULL while their portfolio is their
assignments, not the firm.

`load_visible_client` is the seam every R1 view and service loads through. It is a
function rather than a `get_object_or_404` call site so that there is one place
the rule lives: a client the actor cannot see is a client that does not exist —
404 — because a 403 confirms the id names a real row, which is precisely the fact
the boundary exists to withhold.

Portal code never calls this. `app_portal` is confined by the database to one
client already, and the portal's client identity arrives on the membership the
middleware grants — never as request data this function could be asked to load.
"""

from uuid import UUID

from django.http import Http404

from apps.authz.portfolio import visible_clients
from apps.authz.services import Actor
from apps.clients.models import ClientCompany


def load_visible_client(
    actor: Actor,
    client_id: UUID,
    *,
    tenant_id: UUID | None = None,
) -> ClientCompany:
    """Return the client `client_id` names, only if it is inside the actor's book.

    `tenant_id` follows the convention of `can()` and `visible_clients`: an
    explicit tenant for the callers that run outside a resolved request — a
    management command, a task — while the request path lets the ambient context
    answer.

    Raises `Http404` for everything outside the portfolio, on purpose and
    uniformly: not-found answers the same for a foreign tenant's client, an
    unassigned one, and an id that names nothing at all.
    """
    client = visible_clients(actor, tenant_id=tenant_id).filter(pk=client_id).first()
    if client is None:
        raise Http404
    return client


__all__ = ["load_visible_client"]
