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

from apps.core.views import healthz
from apps.portal.views import document_download, document_upload, portal_home

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("allauth.urls")),
    path("documentos/enviar", document_upload, name="portal-document-upload"),
    path(
        "documentos/<str:storage_key>",
        document_download,
        name="portal-document-download",
    ),
    path("", portal_home, name="portal-home"),
]
