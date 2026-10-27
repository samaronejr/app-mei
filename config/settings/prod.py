"""Production settings: TLS-terminated, host-only cookies, Sentry on."""

import ast
import copy
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from weakref import ref

import sentry_sdk
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotocoreConfig
from celery import _state as celery_state
from django.conf import settings
from django.urls import Resolver404, resolve
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scope import add_global_event_processor
from sentry_sdk.scrubber import DEFAULT_DENYLIST, DEFAULT_PII_DENYLIST, EventScrubber
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint, Log
from sentry_sdk.utils import format_attribute

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


_WITHHELD_EVENT: Final = "axrm011_event_withheld"
_DROPPED_RECORD: Final = "_axrm011_record_dropped"


def drop_unscrubbable_event(event: Mapping[str, object], hint: Hint) -> Event | None:
    """before_send: an event that cannot be scrubbed is dropped, never delivered."""
    try:
        if dict.get(cast("dict[str, Any]", event), _WITHHELD_EVENT):
            return None
        record = hint.get("log_record")
        if record is not None and (
            type(record) is not logging.LogRecord
            or dict.get(record.__dict__, _DROPPED_RECORD)
        ):
            return None
        return scrub_sentry_event(event, hint)
    except Exception:  # noqa: BLE001 - fail closed
        return None


def drop_unscrubbable_breadcrumb(
    breadcrumb: Breadcrumb, hint: BreadcrumbHint
) -> Breadcrumb | None:
    """Scrub before retention, while transient task/delivery values still exist."""
    return cast(
        "Breadcrumb | None",
        drop_unscrubbable_event(breadcrumb, hint),
    )


def drop_unscrubbable_log(log: Log, _hint: Hint) -> Log | None:
    """Freeze SDK attributes before its batcher can render opaque originals."""
    try:
        credentials = _known_values()
        attributes = {
            str(name): format_attribute(value)
            for name, value in log["attributes"].items()
        }
        scrubbed = redact_structure(
            attributes,
            credentials,
            fields=SENTRY_CREDENTIAL_FIELDS,
            urls=_redact_credential_url,
        )
        # "body" here is the SDK's diagnostic message, not a provider payload.
        # Discover private attribute fields first, then replace their echoes in it.
        return cast(
            "Log",
            redact_structure(
                {**log, "attributes": scrubbed},
                credentials,
                fields=None,
                urls=_redact_credential_url,
            ),
        )
    except Exception:  # noqa: BLE001 - nothing unsafe enters the background batcher
        return None


class CredentialEventScrubber(EventScrubber):
    """Discover raw credential leaves before SDK serialization can truncate them."""

    def scrub_event(self, event: Event) -> None:
        # Event is a TypedDict statically; the SDK's in-place hook owns its dict.
        if not issubclass(type(cast("object", event)), dict):
            # Only a dict can be rewritten without calling the carrier's methods.
            # The boundary's processor refuses other carriers before this hook;
            # check-ins skip processors, so here they are left untouched.
            return
        husk = cast("dict[str, object]", cast("object", event))
        try:
            if dict.get(husk, _WITHHELD_EVENT):
                return
            scrubbed = scrub_sentry_event(event, {})
            # The SDK owns a dict, but a subclass can override its mutators or
            # inject fields during discovery. Replace its contents from the safe
            # result using only the non-overridable dict primitives.
            dict.clear(husk)
            dict.update(husk, scrubbed)
        except Exception:  # noqa: BLE001 - fail closed; before_send drops the husk
            dict.clear(husk)
            dict.update(husk, {_WITHHELD_EVENT: True})


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
        if key is None or key == _RESET_FORM_URL_KEY:
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
_TASK_SUCCESS_FIELDS: Final = frozenset(
    {"args", "id", "kwargs", "name", "return_value", "runtime"}
)


def _task_failure_context(
    producer: object, context: object, credentials: set[str]
) -> dict[str, object] | None:
    """Keep Celery task identity visible and hide arguments, results and failures.

    Celery logs each failure's context under ``extra["data"]``, a provider-body name,
    so every leaf of it, key names included, became a known credential and rewrote
    the task name and id and even the filter's own field names. Only Celery's exact
    context shape, from its own logger, is treated this way; anything else stays
    fully hidden. Sentry never sees this record: its Celery integration ignores the
    logger, so only the console filter needs it.
    """
    if producer != _TASK_FAILURE_LOGGER or not isinstance(context, Mapping):
        return None
    names = set(context)
    secrets: tuple[str, ...]
    if names == _TASK_FAILURE_FIELDS:
        secrets = _TASK_FAILURE_SECRETS
    elif names == _TASK_SUCCESS_FIELDS:
        secrets = ("args", "kwargs", "return_value")
    else:
        return None
    # A pair under a hidden name remembers its value, and the literals inside it.
    redact_structure(
        [["token", str(context[name])] for name in secrets],
        credentials,
        fields=SENTRY_CREDENTIAL_FIELDS,
    )
    return {
        name: SENTRY_REDACTION_MARKER if name in secrets else item
        for name, item in context.items()
    }


def _withhold(record: logging.LogRecord, _error: Exception) -> None:
    """Fail closed: replace the whole record, not only its message and extras.

    The production formatter renders standard fields as well (the logger name and
    level). Neither an arbitrary numeric level nor the redactor exception's class
    name is safe metadata: either can itself contain an original credential.
    Read none of the original mapping; replace it with constant diagnostics.
    """
    safe = logging.LogRecord(
        name=SENTRY_REDACTION_MARKER,
        level=logging.ERROR,
        pathname=SENTRY_REDACTION_MARKER,
        lineno=0,
        msg=f"{SENTRY_REDACTION_MARKER} log record withheld: redaction failed",
        args=(),
        exc_info=None,
    )
    safe.levelname = "ERROR"
    object.__setattr__(record, "__dict__", safe.__dict__)


_MIN_TASK_SECRET: Final = 8
_TASK_KNOWN_CACHE: Final = "_axrm011_known_values"
_TASK_KNOWN_OWNER: Final = "_axrm011_known_owner"


class _CeleryTaskStack:
    @property
    def stack(self) -> list[Any] | None:
        # Resolve at the protected use boundary, not while settings import.
        # An unavailable private name must withhold diagnostics, not break boot.
        return cast("list[Any] | None", celery_state._task_stack.stack)  # noqa: SLF001


_task_stack = _CeleryTaskStack()


class _TaskRequest(Protocol):
    args: object
    kwargs: object
    __dict__: dict[str, Any]


def _task_arguments_known(request: _TaskRequest) -> tuple[str, ...]:
    """The long string values a running task was given, learned once per execution."""
    cached = request.__dict__.get(_TASK_KNOWN_CACHE)
    owner = request.__dict__.get(_TASK_KNOWN_OWNER)
    if cached is None or owner is None or owner() is not request:
        given = [
            *(request.args if isinstance(request.args, (list, tuple)) else ()),
            *(request.kwargs.values() if isinstance(request.kwargs, Mapping) else ()),
        ]
        found: set[str] = set()
        redact_structure([["token", given]], found, fields=SENTRY_CREDENTIAL_FIELDS)
        cached = tuple(sorted(item for item in found if len(item) >= _MIN_TASK_SECRET))
        request.__dict__[_TASK_KNOWN_CACHE] = cached
        # Celery copies a parent's dictionary into direct nested requests. A weak
        # identity binding rejects that copied cache without retaining the owner.
        request.__dict__[_TASK_KNOWN_OWNER] = ref(request)
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


# What one log record was scrubbed with, for the length of its dispatch. A value
# found only in the record's own fields (a password extra) is hidden by the first
# pass, yet later passes and the SDK's event, breadcrumb and Log render the live
# exception again from exc_info. All of them run inside that dispatch.
_RECORD_DISPATCH: Final[ContextVar[set[str] | None]] = ContextVar(
    "axrm011_record_dispatch_known_values", default=None
)


def _known_values() -> set[str]:
    return {
        *sensitive_values.get(),
        *_task_known_values(),
        *(_RECORD_DISPATCH.get() or ()),
    }


def _share_with_dispatch(credentials: set[str]) -> None:
    """Later passes and SDK sinks of this dispatch render exc_info again."""
    dispatch = _RECORD_DISPATCH.get()
    if dispatch is not None:
        dispatch.update(credentials)


class CredentialURLLogFilter(logging.Filter):
    """Remove provider and URL credentials before console record serialization."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Only the standard carrier has non-overridable formatting/dict access.
        # Reject custom carriers at every handler, including all SDK handlers.
        if type(record) is not logging.LogRecord:
            return False
        try:
            if dict.get(record.__dict__, _DROPPED_RECORD):
                return False
            # makeLogRecord legitimately defaults these to None. Normalize only
            # those defaults before any dispatcher or formatter reads them.
            if record.name is None:
                record.name = SENTRY_REDACTION_MARKER
            if record.levelno is None:
                record.levelno = logging.ERROR
            if type(record.name) is not str or type(record.levelno) is not int:
                _withhold(record, TypeError())
            record.levelname = str(record.levelname)
            self._scrub(record)
            # Probe the actual console formatter before StreamHandler.handleError
            # could serialize an invalid original record to stderr.
            logging.Formatter(BASE_LOGGING["formatters"]["simple"]["format"]).format(
                record
            )
        except Exception as error:  # noqa: BLE001 - fail closed, a log call never raises
            try:
                _withhold(record, error)
            except Exception:  # noqa: BLE001 - nothing more can be made safe: drop it
                dict.update(record.__dict__, {_DROPPED_RECORD: True})
                return False
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
        object.__setattr__(record, "__dict__", updates)
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
            record.stack_info = scrub_text(str(record.stack_info), credentials)
        if record.exc_info is not None:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text is not None:
            # The whole rendered chain, including causes and contexts.
            record.exc_text = scrub_text(str(record.exc_text), credentials)
        _share_with_dispatch(credentials)


class CredentialLoggingBoundary(Integration):
    """Guard the SDK's outer dispatch and give every event hook a plain dict."""

    identifier = "credential_logging_boundary"

    @staticmethod
    def setup_once() -> None:
        add_global_event_processor(_own_event_carrier)
        previous = logging.Logger.callHandlers

        def call_handlers(self: logging.Logger, record: logging.LogRecord) -> None:
            try:
                boundary = sentry_sdk.get_client().get_integration(
                    CredentialLoggingBoundary
                )
            except Exception:  # noqa: BLE001 - fail closed before selecting a client
                return
            if boundary is None:
                previous(self, record)
                return
            try:
                # Every sink of this record shares what its first pass discovered;
                # a record logged during the dispatch inherits it as well.
                known = _RECORD_DISPATCH.set(set(_RECORD_DISPATCH.get() or ()))
                try:
                    if not CredentialURLLogFilter().filter(record):
                        return
                    previous(self, record)
                finally:
                    _RECORD_DISPATCH.reset(known)
            except Exception:  # noqa: BLE001 - never expose originals from SDK dispatch
                return

        type.__setattr__(logging.Logger, "callHandlers", call_handlers)


def _own_event_carrier(event: Event, _hint: Hint) -> Event | None:
    """Continue with a plain dict the hooks own, or refuse the carrier.

    The scrubber hook rewrites through unbound dict primitives, but the SDK then
    serializes through the carrier's own accessors: a dict subclass could revive
    a husk, and a mapping that is not a dict cannot be rewritten at all. Reading
    a subclass's storage through dict.items calls none of its methods; the
    original keeps only the withheld marker.
    """
    carrier = cast("object", event)
    if type(carrier) is dict:
        return event
    if sentry_sdk.get_client().get_integration(CredentialLoggingBoundary) is None:
        return event
    if not issubclass(type(carrier), dict):
        return None
    original = cast("dict[str, object]", carrier)
    owned = dict(dict.items(original))
    dict.clear(original)
    dict.update(original, {_WITHHELD_EVENT: True})
    return cast("Event", cast("object", owned))


# Logger.callHandlers' SDK patch processes the original record independently of
# console rejection. Put the same ingress filter on every SDK logging sink,
# before it can format an event, retain a breadcrumb, or enqueue a Sentry Log.
_credential_logging = LoggingIntegration()
for _handler in (
    _credential_logging._handler,  # noqa: SLF001 - locked SDK logging sinks
    _credential_logging._breadcrumb_handler,  # noqa: SLF001
    _credential_logging._sentry_logs_handler,  # noqa: SLF001
):
    if _handler is not None:
        _handler.addFilter(CredentialURLLogFilter())


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
    "integrations": [
        DjangoIntegration(),
        CeleryIntegration(),
        _credential_logging,
        CredentialLoggingBoundary(),
    ],
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
    "before_send": drop_unscrubbable_event,
    "before_send_transaction": drop_unscrubbable_event,
    "before_breadcrumb": drop_unscrubbable_breadcrumb,
    "before_send_log": drop_unscrubbable_log,
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
