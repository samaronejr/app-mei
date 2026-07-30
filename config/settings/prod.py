"""Production settings: TLS-terminated, host-only cookies, Sentry on."""

import sentry_sdk
from botocore.config import Config as BotocoreConfig
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
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        # Defaulted to "" rather than required at import, so the module stays
        # importable for inspection. Emptiness is enforced at boot by `core.E011`
        # instead, which names every missing variable at once and, unlike an
        # import-time raise, can itself be tested.
        "OPTIONS": {
            "bucket_name": env.str("OCI_S3_BUCKET", default=""),
            "endpoint_url": env.str("OCI_S3_ENDPOINT_URL", default=""),
            "region_name": env.str("OCI_S3_REGION", default=""),
            # Passed explicitly rather than left to boto3's environment discovery, which
            # would silently fall back to any AWS_* variables that happen to be set.
            "access_key": env.str("OCI_S3_ACCESS_KEY_ID", default=""),
            "secret_key": env.str("OCI_S3_SECRET_ACCESS_KEY", default=""),
            # querystring_auth=False is a DECISION, not a default: with it True, url()
            # mints a pre-signed URL that outlives the session and cannot be revoked
            # inside its lifetime. V4 forbids pre-signed URLs outright, so url() must
            # not be able to produce a working one even if a template calls it.
            "querystring_auth": False,
            # No default_acl. OCI's S3 layer has no per-object ACLs, so sending
            # x-amz-acl is at best ignored; the bucket itself is private and objects
            # inherit that. Setting it would look like a control and be none.
            # A repeated storage key must not silently clobber the earlier document;
            # keys are random, so a collision means something is wrong.
            "file_overwrite": False,
            # One Config object, not the individual signature_version/addressing_style
            # options: django-storages only builds its default config when
            # client_config is None, so those settings would be silently dead here.
            "client_config": BotocoreConfig(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                # OCI rejects aws-chunked transfer encoding, which boto3 >= 1.36 turns
                # on by default through request checksums. Measured: every PutObject
                # fails `NotImplemented: AWS chunked encoding not supported` without
                # this, while auth and HeadObject succeed -- so it looks like a
                # credential problem and is not one.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        },
    },
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
