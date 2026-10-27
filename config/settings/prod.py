"""Production settings: TLS-terminated, host-only cookies, Sentry on."""

import ast
import copy
import logging
from collections.abc import Mapping
from typing import Any, Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotocoreConfig
from celery._state import _task_stack
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
from config.settings.base import LOGGING as BASE_LOGGING
from config.settings.base import env

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
    try:
        parsed = urlsplit(value)
    except ValueError:
        # Known-value replacement can put [Filtered] in a hostname. Invalid URLs
        # are private diagnostics, not a reason to drop the surrounding event.
        return SENTRY_REDACTION_MARKER, {value}
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


def _restore_sentry_scalars(value: object, annotations: object) -> object:
    """Recover non-string scalar reprs on the SDK's bounded annotation paths."""
    if not isinstance(annotations, Mapping):
        return value
    match value:
        case Mapping():
            return {
                key: _restore_sentry_scalars(item, annotations.get(str(key)))
                for key, item in value.items()
            }
        case list():
            return [
                _restore_sentry_scalars(item, annotations.get(str(index)))
                for index, item in enumerate(value)
            ]
        case str():
            details = annotations.get("", {})
            removals = details.get("rem") if isinstance(details, Mapping) else None
            if isinstance(removals, list) and ["!limit", "x"] in removals:
                try:
                    scalar = ast.literal_eval(value)
                except (ValueError, SyntaxError, SyntaxWarning):
                    return value
                match scalar:
                    case int() | float() | None:
                        return scalar
    return value


def scrub_sentry_event(event: Mapping[str, object], _hint: Hint) -> Event:
    credentials = _known_values()
    # SDK annotation paths mirror credential field names, but their leaves are
    # bookkeeping such as "!limit"/"x", not credential values. They still receive
    # every known-value replacement after discovery from the actual event.
    source = {key: item for key, item in event.items() if key != "_meta"}
    scrubbed = cast(
        "Event",
        redact_structure(
            _restore_sentry_scalars(source, event.get("_meta")),
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


_TASK_FAILURE_LOGGER: Final = "celery.app.trace"
_TASK_FAILURE_FIELDS: Final = frozenset(
    {
        "args",
        "description",
        "exc",
        "hostname",
        "id",
        "internal",
        "kwargs",
        "name",
        "traceback",
    }
)
# What a failed task carried or raised; the other fields only identify the task.
_TASK_FAILURE_SECRETS: Final = ("args", "exc", "kwargs", "traceback")
_TASK_FAILURE_ALIAS: Final = "task_failure_context"


def _task_failure_context(
    producer: object, context: object, credentials: set[str]
) -> dict[str, object] | None:
    """Keep a failed task's identity visible and hide what it carried or raised.

    Celery logs each failure's context under ``extra["data"]``, a provider-body name,
    so every leaf of it, key names included, became a known credential and rewrote
    the task name and id and even the filter's own field names. Only Celery's exact
    context shape, from its own logger, is treated this way; anything else stays
    fully hidden. Sentry never sees this record: its Celery integration ignores the
    logger, so only the console filter needs it.
    """
    if (
        producer != _TASK_FAILURE_LOGGER
        or not isinstance(context, Mapping)
        or set(context) != _TASK_FAILURE_FIELDS
    ):
        return None
    # A pair under a hidden name remembers its value, and the literals inside it.
    redact_structure(
        [["token", str(context[name])] for name in _TASK_FAILURE_SECRETS],
        credentials,
        fields=SENTRY_CREDENTIAL_FIELDS,
    )
    return {
        name: SENTRY_REDACTION_MARKER if name in _TASK_FAILURE_SECRETS else item
        for name, item in context.items()
    }


def _withhold(record: logging.LogRecord, error: Exception) -> None:
    """Fail closed: replace the whole record, not only its message and extras.

    The production formatter renders standard fields as well (the logger name and
    level), so a record that could not be scrubbed keeps nothing it arrived with but
    a numeric level.
    """
    level = record.__dict__.get("levelno")
    safe = logging.LogRecord(
        name=SENTRY_REDACTION_MARKER,
        level=level if type(level) is int else logging.ERROR,
        pathname=SENTRY_REDACTION_MARKER,
        lineno=0,
        msg=(
            f"{SENTRY_REDACTION_MARKER} log record withheld: "
            f"redaction failed ({type(error).__name__})"
        ),
        args=(),
        exc_info=None,
    )
    record.__dict__.clear()
    record.__dict__.update(safe.__dict__)


_MIN_TASK_SECRET: Final = 8
_TASK_KNOWN_CACHE: Final = "_axrm011_known_values"


class _TaskRequest(Protocol):
    args: object
    kwargs: object
    __dict__: dict[str, Any]


def _task_arguments_known(request: _TaskRequest) -> tuple[str, ...]:
    """The long string values a running task was given, learned once per execution."""
    cached = request.__dict__.get(_TASK_KNOWN_CACHE)
    if cached is None:
        given = [
            *(request.args if isinstance(request.args, (list, tuple)) else ()),
            *(request.kwargs.values() if isinstance(request.kwargs, Mapping) else ()),
        ]
        found: set[str] = set()
        redact_structure([["token", given]], found, fields=SENTRY_CREDENTIAL_FIELDS)
        cached = tuple(sorted(item for item in found if len(item) >= _MIN_TASK_SECRET))
        request.__dict__[_TASK_KNOWN_CACHE] = cached
    return cast("tuple[str, ...]", cached)


def _task_known_values() -> tuple[str, ...]:
    """Argument values of the tasks running on this thread, owned by their requests.

    Celery pushes a request for every execution and pops it itself, signal or not, so
    these values cannot outlive their run, be swapped between overlapping executions
    that share a task id, or survive because a cleanup signal never arrived. Nested
    tasks compose; the inherited delivery context stays a separate source.
    """
    known: list[str] = []
    seen: set[int] = set()
    for task in _task_stack.stack or ():
        if id(task) in seen:
            continue
        seen.add(id(task))
        for request in task.request_stack.stack or ():
            known.extend(_task_arguments_known(request))
    return tuple(known)


def _known_values() -> set[str]:
    return {*sensitive_values.get(), *_task_known_values()}


class CredentialURLLogFilter(logging.Filter):
    """Remove provider and URL credentials before console record serialization."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub(record)
        except Exception as error:  # noqa: BLE001 - fail closed, a log call never raises
            _withhold(record, error)
        return True

    def _scrub(self, record: logging.LogRecord) -> None:
        credentials = _request_credentials(record) | _known_values()
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
        extras = {**record.__dict__, "msg": message}
        task_context = _task_failure_context(
            record.name, extras.get("data"), credentials
        )
        if task_context is not None:
            extras["data"] = task_context
        # Fields travel as positional [name, value] pairs, so a name that discovery
        # rewrites or merges ("exc" inside "exception_arguments") can neither lose a
        # field nor leave its original value in place.
        pairs: list[list[object]] = [
            [
                _TASK_FAILURE_ALIAS
                if name == "data" and task_context is not None
                else name,
                value,
            ]
            for name, value in extras.items()
        ]
        pairs.append(["exception_arguments", [error.args for error in chain]])
        pairs.append(
            [
                "exception_notes",
                [getattr(error, "__notes__", ()) for error in chain],
            ]
        )
        scrubbed = cast(
            "list[list[object]]",
            redact_structure(pairs, credentials, fields=SENTRY_CREDENTIAL_FIELDS),
        )
        *fields, arguments, _notes = (pair[1] for pair in scrubbed)
        updates = dict(zip(extras, fields, strict=True))
        replacements = list(
            zip(chain, cast("list[tuple[Any, ...]]", arguments), strict=True)
        )
        record.__dict__.update(updates)
        for exception, replacement in replacements:
            exception.args = replacement
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
LOGGING = copy.deepcopy(BASE_LOGGING)
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
