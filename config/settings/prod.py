"""Production settings: TLS-terminated, host-only cookies, Sentry on."""

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

from config.settings.base import *  # noqa: F403
from config.settings.base import env

DEBUG = False

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# The __Host- prefix is only honoured by browsers when the cookie is Secure, Path=/
# and carries no Domain attribute. It is therefore prod-only: it structurally defeats
# cookie tossing from a sibling subdomain, which is the attack host-only cookies exist
# to stop. Dev runs over plain HTTP, where the browser would reject the prefixed name.
SESSION_COOKIE_NAME = "__Host-sessionid"
CSRF_COOKIE_NAME = "__Host-csrftoken"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

_sentry_dsn = env.str("SENTRY_DSN", default="")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[DjangoIntegration()],
        environment=env.str("SENTRY_ENVIRONMENT", default="production"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        send_default_pii=False,
    )
