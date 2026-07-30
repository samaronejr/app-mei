"""The portal landing page -- the tracer bullet for the whole isolation stack.

This view is deliberately thin, because what it proves is not its own logic. It
runs as `app_portal` inside `PortalMiddleware`'s transaction, so every row it
renders has already passed the RESTRICTIVE policies comparing `app.client_id`.
If the isolation is wrong this page shows another firm's client or the wrong
obligation count; if it is right, the code below needs no filtering of its own.

**Every table touched here must be one of the six `app_portal` holds SELECT
on.** `request.client` and `request.user` are already materialised by the
middleware and cost no query. `clients_clientcompany` and
`obligations_obligation` are both allow-listed. Reaching for `{{ perms.* }}`
(`auth_permission`), `tenants_tenant` or `mfa_authenticator` from the template
raises `permission denied`, which is why the template renders `request.client`
rather than `request.tenant`.
"""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.obligations.models import Obligation


@login_required
def portal_home(request: HttpRequest) -> HttpResponse:
    """Render the signed-in client's own company and its obligation count."""
    client = getattr(request, "client", None)
    # No client filter is applied on purpose. The RESTRICTIVE portal policy on
    # obligations_obligation compares app.client_id, so this count is already confined
    # to the one client — and writing `.filter(client=client)` here would make the test
    # that drops the policy pass anyway, hiding the very failure it exists to catch.
    obligation_count = Obligation.objects.count()
    return render(
        request,
        "portal/home.html",
        {"client": client, "obligation_count": obligation_count},
    )
