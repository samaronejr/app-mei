"""Production settings: TLS-terminated, host-only cookies, Sentry on."""

import logging
from collections.abc import Mapping
from typing import Any, Final, cast
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotocoreConfig
from django.conf import settings
from django.urls import Resolver404, resolve
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, DEFAULT_PII_DENYLIST, EventScrubber
from sentry_sdk.types import Event, Hint

from apps.core.release import UNKNOWN_RELEASE, current_release
from apps.delivery.privacy import sensitive_values
from apps.integrations.privacy import redact_structure, render_structure, scrub_text
from config.settings.base import *  # noqa: F403
from config.settings.base import LOGGING, env

SENTRY_REDACTION_MARKER: Final = "[Filtered]"
SENTRY_DSN_ERROR: Final = (
    "SENTRY_DSN is set but is not a DSN URL; unset it or provide the project DSN"
)
SENTRY_CREDENTIAL_FIELDS: Final = frozenset(
    {
        *DEFAULT_DENYLIST,
        *DEFAULT_PII_DENYLIST,
        "code",
        "confirmation_code",
        "key",
        "login_code",
        "oldpassword",
        "password1",
        "password2",
        "recovery-code",
        "recovery_code",
        "recipient",
        "recipients",
        "recipients_json",
        "to",
        "cc",
        "bcc",
        "body",
        "body_ptbr",
        "body_ref",
        "frozen_mime",
        "mime_base64",
        "subject",
        "email_body",
        "decision_note",
    }
)
_RESET_FROM_KEY_URL_NAME: Final = "account_reset_password_from_key"
_RESET_FORM_URL_KEY: Final = "set-password"


def _replace_path_segment(path: str, credential: str, replacement: str) -> str:
    segments = path.split("/")
    try:
        index = segments.index(credential)
    except ValueError:
        return path
    segments[index] = replacement
    return "/".join(segments)


def _redact_credential_url(value: str) -> tuple[str, set[str]]:
    if not value.startswith(("/", "https://", "http://")):
        return value, set()
    parsed = urlsplit(value)
    if not parsed.path.startswith("/"):
        return value, set()
    try:
        match = resolve(parsed.path, urlconf=settings.ROOT_URLCONF)
    except Resolver404:
        return value, set()
    if match.url_name not in settings.CREDENTIAL_BEARING_URL_NAMES:
        return value, set()

    path = parsed.path
    credentials: set[str] = set()
    parameters = {name: str(item) for name, item in match.kwargs.items()}
    if match.url_name == _RESET_FROM_KEY_URL_NAME:
        uidb36 = parameters.get("uidb36")
        key = parameters.get("key")
        if key == _RESET_FORM_URL_KEY:
            return value, set()
        if uidb36 is not None and key is not None:
            combined = f"{uidb36}-{key}"
            credentials.update({key, combined})
            path = _replace_path_segment(
                path,
                combined,
                f"{uidb36}-{SENTRY_REDACTION_MARKER}",
            )
    else:
        credentials.update(parameters.values())
        for credential in credentials:
            path = _replace_path_segment(
                path,
                credential,
                SENTRY_REDACTION_MARKER,
            )

    redacted = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return redacted, credentials


def scrub_sentry_event(event: Mapping[str, object], _hint: Hint) -> Event:
    credentials: set[str] = set(sensitive_values.get())
    # SDK annotation paths mirror credential field names, but their leaves are
    # bookkeeping such as "!limit"/"x", not credential values. They still receive
    # every known-value replacement after discovery from the actual event.
    source = {key: item for key, item in event.items() if key != "_meta"}
    scrubbed = cast(
        "Event",
        redact_structure(
            source,
            credentials,
            fields=SENTRY_CREDENTIAL_FIELDS,
            urls=_redact_credential_url,
        ),
    )
    if "_meta" in event:
        annotations = redact_structure(
            {"_meta": event["_meta"]}, credentials, fields=None
        )
        scrubbed.update(cast("Event", annotations))
    return scrubbed


def scrub_sentry_transaction(event: Event, hint: Hint) -> Event:
    return scrub_sentry_event(event, hint)


class CredentialEventScrubber(EventScrubber):
    """Discover raw credential leaves before SDK serialization can truncate them."""

    def scrub_event(self, event: Event) -> None:
        scrubbed = scrub_sentry_event(event, {})
        event.update(scrubbed)


def _request_credentials(record: logging.LogRecord) -> set[str]:
    request = getattr(record, "request", None)
    if request is None:
        return set()
    try:
        match = resolve(
            request.path_info,
            urlconf=getattr(request, "urlconf", settings.ROOT_URLCONF),
        )
    except Resolver404:
        return set()
    if match.url_name not in settings.CREDENTIAL_BEARING_URL_NAMES:
        return set()
    parameters = {name: str(item) for name, item in match.kwargs.items()}
    if match.url_name == _RESET_FROM_KEY_URL_NAME:
        key = parameters.get("key")
        if key in {None, _RESET_FORM_URL_KEY}:
            return set()
        uidb36 = parameters.get("uidb36")
        return {key, f"{uidb36}-{key}" if uidb36 is not None else key}
    return set(parameters.values())


def _exception_chain(exception: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    pending = [exception]
    while pending:
        current = pending.pop()
        if current is None or any(current is seen for seen in chain):
            continue
        chain.append(current)
        pending.extend((current.__cause__, current.__context__))
    return chain


class CredentialURLLogFilter(logging.Filter):
    """Remove provider and URL credentials before console record serialization."""

    def filter(self, record: logging.LogRecord) -> bool:
        credentials = _request_credentials(record) | set(sensitive_values.get())
        # Interpolation is in-memory only. Scrubbing a template's %s first would
        # destroy its placeholders and orphan the unlabelled argument values.
        template = record.msg
        message = template
        if isinstance(message, str):
            try:
                message = record.getMessage()
            except (TypeError, ValueError, KeyError, RecursionError):
                message = f"{record.msg} {render_structure(record.args)}"
        # One record-wide discovery includes arguments, extras and every exception
        # before rendering can lose structure or a sibling field can be discarded.
        chain = _exception_chain(record.exc_info[1]) if record.exc_info else []
        source = {
            **record.__dict__,
            "msg": message,
            "exception_arguments": [exception.args for exception in chain],
            "exception_notes": [
                getattr(exception, "__notes__", ()) for exception in chain
            ],
        }
        scrubbed = cast(
            "dict[str, Any]",
            redact_structure(source, credentials, fields=SENTRY_CREDENTIAL_FIELDS),
        )
        record.__dict__.update(
            (key, value)
            for key, value in scrubbed.items()
            if key not in {"exception_arguments", "exception_notes"}
        )
        for exception, arguments in zip(
            chain, scrubbed["exception_arguments"], strict=True
        ):
            exception.args = arguments
        if isinstance(template, str):
            record.msg = template
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError, KeyError, RecursionError):
            rendered = f"{render_structure(record.msg)} {render_structure(record.args)}"
        record.args = ()
        record.msg = scrub_text(rendered, credentials)
        if record.stack_info is not None:
            record.stack_info = scrub_text(record.stack_info, credentials)
        if record.exc_info is not None:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text is not None:
            # The whole rendered chain, including causes and contexts.
            record.exc_text = scrub_text(record.exc_text, credentials)
        return True


DEBUG = False

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env.str("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
DEFAULT_FROM_EMAIL = env.str(
    "DEFAULT_FROM_EMAIL",
    default="nao-responda@samaronefialho.dev",
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
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
            "max_memory_size": 16 * 1024 * 1024,
            "transfer_config": TransferConfig(
                max_concurrency=1,
                max_io_queue=1,
                io_chunksize=65536,
                use_threads=False,
            ),
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
SENTRY_OPTIONS: dict[str, Any] = {
    "integrations": [DjangoIntegration(), CeleryIntegration()],
    "environment": env.str("SENTRY_ENVIRONMENT", default="production"),
    "traces_sample_rate": env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
    "send_default_pii": False,
    # Disabling frame variables removes values from every frame, including most
    # worker traceback diagnostics. Celery widens what a frame can hold; pilot
    # privacy deliberately wins that trade.
    "include_local_variables": False,
    "event_scrubber": CredentialEventScrubber(
        denylist=[*DEFAULT_DENYLIST, *sorted(SENTRY_CREDENTIAL_FIELDS)],
        recursive=True,
        send_default_pii=False,
    ),
    "before_send": scrub_sentry_event,
    "before_send_transaction": scrub_sentry_transaction,
}
_release = current_release()
if _release != UNKNOWN_RELEASE:
    SENTRY_OPTIONS["release"] = _release
LOGGING.setdefault("filters", {})["credential_urls"] = {
    "()": CredentialURLLogFilter,
}
LOGGING["handlers"]["console"]["filters"] = ["credential_urls"]
if _sentry_dsn:
    _parsed_sentry_dsn = urlsplit(_sentry_dsn)
    if (
        _parsed_sentry_dsn.scheme not in {"http", "https"}
        or "@" not in _parsed_sentry_dsn.netloc
        or not _parsed_sentry_dsn.path
    ):
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(SENTRY_DSN_ERROR)
    sentry_sdk.init(dsn=_sentry_dsn, **SENTRY_OPTIONS)
