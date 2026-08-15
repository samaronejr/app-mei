"""The URL tree served on `<slug>-portal.<domain>`.

Deliberately a *subset* of `ROOT_URLCONF`, not a parallel invention. Three
constraints force the shape:

* **allauth mounts at the identical path.** `RATELIMIT_PUBLIC_POST_URL_NAMES`
  matches by `url_name`; `ACCESS_LOG_EXEMPT_PREFIXES` and the `EXEMPT_PREFIXES`
  in `apps/accounts/mfa.py` match by path prefix. Mount the tree anywhere else
  and the credential rate limit stops applying to the portal login form --
  silently, because nothing errors when a limiter simply never matches.
* **The MFA tree must be reachable.** `MFAEnforcementMiddleware` redirects a
  non-enrolled user to `mfa_activate_totp`. That `reverse()` runs in a
  request-phase middleware, so it resolves against `ROOT_URLCONF` and succeeds
  regardless -- but the redirect then lands on the portal host, and if this tree
  omits the path the user gets a 404 or a redirect loop. An earlier revision of
  the plan predicted `NoReverseMatch`; that is wrong, and a test asserting "not
  a 500" would pass against the loop.
* **The firm's own views are absent.** `app_portal` holds SELECT on six tables,
  so most firm views would raise `permission denied` rather than leak -- but
  relying on the database to refuse what routing should never have offered is
  the wrong order of defence.

`/admin/` is absent here and `PortalMiddleware` refuses the prefix outright,
because `AdminTenantMiddleware` is inert on this host.
"""

from django.urls import include, path

from apps.core.views import healthz, readyz, versionz
from apps.portal.invite_views import portal_invite_accept
from apps.portal.views import (
    document_download,
    document_upload,
    portal_account,
    portal_documents,
    portal_home,
    portal_payments,
)

# The four top-level destinations the portal's navigation bar names, and they are
# registered together as one set on purpose. The bar is included by the shell, so it is
# drawn on every portal screen, and `{% url %}` raises `NoReverseMatch` at render time
# for a name this tree does not carry -- which makes a missing route a 500 on the whole
# portal rather than one broken link on one page.
#
# Three of them answer with a placeholder today. Later waves replace the view body and
# the page template IN PLACE and must NOT register these names a second time: a
# duplicate `path()` does not error, it shadows, and `reverse()` then answers with
# whichever pattern happens to come first.
urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("versionz", versionz, name="versionz"),
    path("accounts/", include("allauth.urls")),
    path("pagamentos/", portal_payments, name="portal-payments"),
    path("documentos/", portal_documents, name="portal-documents"),
    path("documentos/enviar", document_upload, name="portal-document-upload"),
    # Last of the three `documentos` routes, because it is the only one that matches a
    # pattern. `<str:...>` takes one or more non-slash characters, so `documentos/`
    # itself can never reach it -- but `documentos/enviar` can, and the upload endpoint
    # is a POST while this is a GET, so shadowing it would surface as a 405.
    path(
        "documentos/<str:storage_key>",
        document_download,
        name="portal-document-download",
    ),
    path("conta/", portal_account, name="portal-account"),
    # Not a navigation destination and deliberately absent from the bar: it is reachable
    # only by holding a token, and an invitee is anonymous when they arrive. The prefix
    # is `convites/` because `PortalMiddleware.INVITE_PREFIX` matches on exactly that
    # string -- move the path and the route keeps resolving while the role and the
    # membership gate silently come back, which is a 500 for every acceptance.
    path(
        "convites/aceitar/<str:token>/",
        portal_invite_accept,
        name="portal-invite-accept",
    ),
    path("", portal_home, name="portal-home"),
]
