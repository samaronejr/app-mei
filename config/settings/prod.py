"""Production settings: TLS-terminated, host-only cookies, Sentry on."""

import ast
import copy
import hashlib
import json
import logging
import sys
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from functools import partial
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from weakref import ref

import sentry_sdk
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotocoreConfig
from celery import _state as celery_state
from django.conf import settings
from django.core.handlers.asgi import ASGIRequest
from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpRequest
from django.urls import Resolver404, resolve
from sentry_sdk import logger as sdk_logger
from sentry_sdk import scope as sentry_scope
from sentry_sdk import utils as sentry_utils
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations import celery as sentry_celery
from sentry_sdk.integrations import logging as sentry_logging
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scope import ScopeType
from sentry_sdk.scrubber import DEFAULT_DENYLIST, DEFAULT_PII_DENYLIST, EventScrubber
from sentry_sdk.session import Session
from sentry_sdk.tracing import Span, Transaction
from sentry_sdk.tracing_utils import Baggage, PropagationContext, has_tracing_enabled
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint, Log
from sentry_sdk.utils import format_attribute

from apps.core.release import UNKNOWN_RELEASE, current_release
from apps.delivery.privacy import sensitive_values
from apps.integrations.privacy import (
    diagnostic_type,
    discover_structure,
    project_structure,
    redact_structure,
    render_structure,
    scrub_text,
)
from config.settings.base import *  # noqa: F403
from config.settings.base import LOGGING as BASE_LOGGING
from config.settings.base import env

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

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
_EXC_INFO_LENGTH: Final = 3
type _ExceptionInfo = (
    tuple[type[BaseException], BaseException, TracebackType | None]
    | tuple[None, None, None]
)


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
    event = cast("dict[str, Any]", project_structure(_plain_storage(event)))
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
    if "exception" in scrubbed and "fingerprint" not in scrubbed:
        identity = [
            [item.get("module"), item.get("type"), item.get("stacktrace")]
            for item in scrubbed["exception"].get("values", [])
        ]
        scrubbed["fingerprint"] = [
            "axrm011",
            hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        ]
    return scrubbed


_WITHHELD_EVENT: Final = "axrm011_event_withheld"
_DROPPED_RECORD: Final = "_axrm011_record_dropped"
_RECORD_STACK: Final = "_axrm011_stack_frames"


def drop_unscrubbable_event(event: Mapping[str, object], hint: Hint) -> Event | None:
    """before_send: an event that cannot be scrubbed is dropped, never delivered."""
    try:
        if type(event) is not dict:
            return None
        if _plain_storage(event).get(_WITHHELD_EVENT) is True:
            return None
        record = _plain_storage(hint).get("log_record")
        if record is not None and (
            type(record) is not logging.LogRecord
            or _plain_storage(record.__dict__).get(_DROPPED_RECORD) is True
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
        log = cast("Log", project_structure(_plain_storage(log)))
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
            # Capture ingress refuses other carriers; a direct hook call leaves
            # them untouched rather than passing them to a dict primitive.
            return
        husk = cast("dict[str, object]", cast("object", event))
        try:
            if _plain_storage(husk).get(_WITHHELD_EVENT) is True:
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
    request = _plain_storage(record.__dict__).get("request")
    if (
        type(request) is not HttpRequest
        and type(request) is not WSGIRequest
        and type(request) is not ASGIRequest
    ):
        return set()
    attributes = _plain_storage(object.__getattribute__(request, "__dict__"))
    path = attributes.get("path_info", "")
    path = path if type(path) is str else ""
    try:
        match = resolve(
            path,
            urlconf=settings.ROOT_URLCONF,
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
        pending.extend(
            (
                BaseException.__dict__["__cause__"].__get__(current),
                BaseException.__dict__["__context__"].__get__(current),
            )
        )
        with suppress(TypeError):
            pending.extend(BaseExceptionGroup.__dict__["exceptions"].__get__(current))
    return chain


def _plain_storage(value: object) -> dict[str, Any]:
    """Read a dict carrier's string fields without invoking any of its hooks."""
    if not issubclass(type(value), dict):
        raise TypeError
    return {
        str.__str__(key): item
        for key, item in dict.items(cast("dict[Any, Any]", value))
        if issubclass(type(key), str)
    }


def _project_mapping(value: object) -> dict[Any, Any]:
    if not issubclass(type(value), dict):
        raise TypeError
    pairs = project_structure(list(dict.items(cast("dict[Any, Any]", value))))
    return dict(cast("list[tuple[object, object]]", pairs))


def _stack_frames() -> list[list[Any]]:
    frames = []
    frame: FrameType | None = sys._getframe(1)  # noqa: SLF001 - code metadata only
    while frame is not None:
        code = frame.f_code
        frames.append([code.co_filename, code.co_name, frame.f_lineno])
        frame = frame.f_back
    return list(reversed(frames))


def _record_source(
    record: logging.LogRecord,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[TracebackType | None]]:
    original = _plain_storage(record.__dict__)
    # Normalize defaults in owned storage, not by looking up original attributes:
    # even an exact LogRecord can hold subclassed keys in its native dictionary.
    if original.get("name") is None:
        original["name"] = SENTRY_REDACTION_MARKER
    if original.get("levelno") is None:
        original["levelno"] = logging.ERROR
    exc_info = original.get("exc_info")
    nodes, traces = _exception_snapshot(exc_info) if exc_info is not None else ([], [])
    empty_info = (
        type(exc_info) is tuple
        and len(exc_info) == _EXC_INFO_LENGTH
        and all(part is None for part in exc_info)
    )
    original["exc_info"] = (None, None, None) if empty_info else None
    extras = cast("dict[str, Any]", project_structure(original))
    if (
        extras.get("stack_info") is not None or empty_info
    ) and _RECORD_STACK not in extras:
        extras[_RECORD_STACK] = _stack_frames()
    return extras, nodes, traces


def _exception_snapshot(
    exc_info: object,
) -> tuple[list[dict[str, Any]], list[TracebackType | None]]:
    if type(exc_info) is not tuple or len(exc_info) != _EXC_INFO_LENGTH:
        raise TypeError
    if exc_info[1] is None:
        return [], []
    chain = _exception_chain(exc_info[1])
    indices = {id(error): index for index, error in enumerate(chain)}
    nodes: list[dict[str, Any]] = []
    traces: list[TracebackType | None] = []
    for index, error in enumerate(chain):
        module, name = diagnostic_type(error)
        attributes = _plain_storage(BaseException.__dict__["__dict__"].__get__(error))
        trace = (
            exc_info[2]
            if index == 0
            else BaseException.__dict__["__traceback__"].__get__(error)
        )
        if trace is not None and type(trace) is not TracebackType:
            # Celery supplies a pickleable traceback proxy; use only the real
            # exception traceback, never the proxy's frame/accessor protocol.
            trace = BaseException.__dict__["__traceback__"].__get__(error)
        traces.append(trace)
        frames = []
        cursor = trace
        while cursor is not None:
            code = cursor.tb_frame.f_code
            frames.append(
                {
                    "filename": code.co_filename,
                    "function": code.co_name,
                    "lineno": cursor.tb_lineno,
                }
            )
            cursor = cursor.tb_next
        cause = BaseException.__dict__["__cause__"].__get__(error)
        context = BaseException.__dict__["__context__"].__get__(error)
        try:
            children = BaseExceptionGroup.__dict__["exceptions"].__get__(error)
        except TypeError:
            children = ()
        try:
            errno = OSError.__dict__["errno"].__get__(error)
        except TypeError:
            errno = attributes.get("_axrm011_errno")
        nodes.append(
            {
                "module": module,
                "type": name,
                "args": project_structure(
                    BaseException.__dict__["args"].__get__(error)
                ),
                "notes": project_structure(attributes.get("__notes__", [])),
                "frames": project_structure(attributes.get("_axrm011_frames", frames)),
                "errno": project_structure(errno),
                "cause": indices.get(id(cause)),
                "context": indices.get(id(context)),
                "suppressed": BaseException.__dict__["__suppress_context__"].__get__(
                    error
                ),
                "children": [indices[id(child)] for child in children],
            }
        )
    return nodes, traces


def _exception_message(node: dict[str, Any]) -> str:
    args = node["args"]
    if type(args) is tuple and len(args) == 1 and type(args[0]) is str:
        return args[0]
    return render_structure(args) if args else ""


def _exception_texts(nodes: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            node["module"],
            node["type"],
            node["args"],
            node["notes"],
            [
                [frame["filename"], frame["function"], frame["lineno"]]
                for frame in node["frames"]
            ],
            node["errno"],
        ]
        for node in nodes
    ]


def _restore_exception_nodes(
    nodes: list[dict[str, Any]], texts: list[list[Any]]
) -> list[dict[str, Any]]:
    # These names describe the schema, not application-supplied mapping keys.
    # A short private argument such as "ex" must not rename "context".
    return [
        {
            **node,
            "module": row[0],
            "type": row[1],
            "args": row[2],
            "notes": row[3],
            "frames": [
                {"filename": frame[0], "function": frame[1], "lineno": frame[2]}
                for frame in row[4]
            ],
            "errno": row[5],
        }
        for node, row in zip(nodes, texts, strict=True)
    ]


def _synthetic_exception_info(
    nodes: list[dict[str, Any]], traces: list[TracebackType | None]
) -> _ExceptionInfo:
    """The SDK and formatters receive no application exception or attribute."""
    if not nodes:
        return None, None, None
    errors: dict[int, BaseException] = {}
    remaining = {index: len(node["children"]) for index, node in enumerate(nodes)}
    parents: dict[int, list[int]] = {}
    for index, node in enumerate(nodes):
        for child in node["children"]:
            parents.setdefault(child, []).append(index)
    ready = [index for index, count in remaining.items() if not count]
    # Group membership is immutable and acyclic; cause/context links may cycle.
    # Construct leaves before groups, then attach all chain links in a second pass.
    while ready:
        index = ready.pop()
        node = nodes[index]
        base = ExceptionGroup if node["children"] else Exception
        kind = type(node["type"], (base,), {"__module__": node["module"]})
        message = _exception_message(node)
        errors[index] = (
            kind(message, [errors[child] for child in node["children"]])
            if node["children"]
            else kind(message)
        )
        error = errors[index]
        BaseException.__dict__["__traceback__"].__set__(error, traces[index])
        attributes = BaseException.__dict__["__dict__"].__get__(error)
        dict.update(
            attributes,
            {
                "__notes__": node["notes"],
                "_axrm011_frames": node["frames"],
                "_axrm011_errno": node["errno"],
            },
        )
        for parent in parents.get(index, []):
            remaining[parent] -= 1
            if not remaining[parent]:
                ready.append(parent)
    for index, node in enumerate(nodes):
        error = errors[index]
        BaseException.__dict__["__cause__"].__set__(error, errors.get(node["cause"]))
        BaseException.__dict__["__context__"].__set__(
            error, errors.get(node["context"])
        )
        BaseException.__dict__["__suppress_context__"].__set__(
            error, node["suppressed"]
        )
    return type(errors[0]), errors[0], traces[0]


def _safe_exception_info(
    exc_info: object,
) -> _ExceptionInfo:
    if exc_info is None or exc_info is True:
        exc_info = sys.exc_info()
    elif type(exc_info) is not tuple:
        exc_info = (
            type(exc_info),
            exc_info,
            BaseException.__dict__["__traceback__"].__get__(exc_info),
        )
    nodes, traces = _exception_snapshot(exc_info)
    # Freeze, do not redact yet: scope fields have not been merged. Their ranges
    # must be unioned with these original primitive strings in the raw scrubber.
    return _synthetic_exception_info(nodes, traces)


def _exception_values(exc_info: object, mechanism: object) -> list[dict[str, Any]]:
    nodes, _ = _exception_snapshot(exc_info)
    values = []
    projected_mechanism = project_structure(mechanism)
    for index, node in enumerate(nodes):
        details = {
            **(projected_mechanism if type(projected_mechanism) is dict else {}),
            "exception_id": index,
        }
        for parent, candidate in enumerate(nodes):
            if index in candidate["children"]:
                details.update(
                    parent_id=parent,
                    source=f"exceptions[{candidate['children'].index(index)}]",
                )
            elif candidate["cause"] == index:
                details.update(source="__cause__")
            elif candidate["context"] == index:
                details.update(source="__context__")
        if node["errno"] is not None:
            details["meta"] = {"errno": {"number": node["errno"]}}
        values.append(
            {
                "module": node["module"],
                "type": node["type"],
                "value": _exception_message(node)
                + "".join("\n" + str(note) for note in node["notes"]),
                "stacktrace": {"frames": node["frames"]},
                "mechanism": details,
            }
        )
    return list(reversed(values))


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
    producer: object, context: object
) -> tuple[dict[str, object], tuple[object, ...]] | None:
    """Keep Celery task identity visible and hide arguments, results and failures.

    Celery logs each failure's context under ``extra["data"]``, a provider-body name,
    so every leaf of it, key names included, became a known credential and rewrote
    the task name and id and even the filter's own field names. Only Celery's exact
    context shape, from its own logger, is treated this way; anything else stays
    fully hidden. Sentry never sees this record: its Celery integration ignores the
    logger, so only the console filter needs it.
    """
    if producer != _TASK_FAILURE_LOGGER or type(context) is not dict:
        return None
    names = set(context)
    secrets: tuple[str, ...]
    if names == _TASK_FAILURE_FIELDS:
        secrets = _TASK_FAILURE_SECRETS
    elif names == _TASK_SUCCESS_FIELDS:
        secrets = ("args", "kwargs", "return_value")
    else:
        return None
    return (
        {
            name: SENTRY_REDACTION_MARKER if name in secrets else item
            for name, item in context.items()
        },
        tuple(context[name] for name in secrets),
    )


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
        positional = project_structure(request.args)
        keyword = project_structure(request.kwargs)
        given = [
            *(
                positional
                if type(positional) is list or type(positional) is tuple
                else ()
            ),
            *(keyword.values() if type(keyword) is dict else ()),
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


def _known_values() -> set[str]:
    return {
        *(str.__str__(value) for value in sensitive_values.get()),
        *_task_known_values(),
    }


class CredentialURLLogFilter(logging.Filter):
    """Remove provider and URL credentials before console record serialization."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Only the standard carrier has non-overridable formatting/dict access.
        # Reject custom carriers at every handler, including all SDK handlers.
        if type(record) is not logging.LogRecord:
            return False
        try:
            original = _plain_storage(record.__dict__)
            if original.get(_DROPPED_RECORD) is True:
                return False
            # makeLogRecord legitimately defaults these to None. _record_source
            # normalizes those defaults in its owned copy before any formatter.
            name, level = original.get("name"), original.get("levelno")
            if (name is not None and type(name) is not str) or (
                level is not None and type(level) is not int
            ):
                _withhold(record, TypeError())
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
        credentials = _known_values()
        credentials.update(_request_credentials(record))
        extras, nodes, traces = _record_source(record)
        template = extras["msg"]
        context = _task_failure_context(extras["name"], extras.get("data"))
        task_context, private = context if context is not None else (None, ())
        if task_context is not None:
            extras["data"] = task_context
        # Nothing after this projection can call an application's renderer,
        # accessor or protocol hook. Credentials belong only to this local walk.
        pairs: list[list[object]] = [
            [
                _TASK_FAILURE_ALIAS
                if name == "data" and task_context is not None
                else name,
                value,
            ]
            for name, value in extras.items()
        ]
        pairs.append(["exception_snapshot", _exception_texts(nodes)])
        discover_structure(
            [*pairs, ["token", private]],
            credentials,
            fields=SENTRY_CREDENTIAL_FIELDS,
        )
        redact_structure(
            [["token", render_structure(value)] for value in private],
            credentials,
            fields=SENTRY_CREDENTIAL_FIELDS,
        )
        message = str(template)
        try:
            if extras["args"]:
                message %= extras["args"]
        except (TypeError, ValueError, KeyError, RecursionError):
            message = f"{message} {render_structure(extras['args'])}"
        pairs[list(extras).index("msg")][1] = message
        pairs[list(extras).index("levelname")][1] = str(extras["levelname"])
        # Fields travel as positional [name, value] pairs, so a name that discovery
        # rewrites or merges ("exc" inside "exception_arguments") can neither lose a
        # field nor leave its original value in place.
        scrubbed = cast(
            "list[list[object]]",
            redact_structure(pairs, credentials, fields=SENTRY_CREDENTIAL_FIELDS),
        )
        *fields, snapshot = (pair[1] for pair in scrubbed)
        updates = dict(zip(extras, fields, strict=True))
        # Render only the redacted primitive arguments. Rendering the original
        # container first can consume a credential-bearing key's following value.
        rendered = str(template)
        try:
            if updates["args"]:
                rendered %= updates["args"]
        except (TypeError, ValueError, KeyError, RecursionError):
            rendered = f"{rendered} {render_structure(updates['args'])}"
        updates["msg"] = scrub_text(rendered, credentials)
        snapshot = _restore_exception_nodes(nodes, cast("list[list[Any]]", snapshot))
        if nodes:
            updates["exc_info"] = _synthetic_exception_info(snapshot, traces)
            lines: list[str] = []
            for node in reversed(snapshot):
                lines.extend(
                    f'  File "{frame["filename"]}", line {frame["lineno"]}, '
                    f"in {frame['function']}"
                    for frame in node["frames"]
                )
                lines.append(
                    f"{node['module']}.{node['type']}: {_exception_message(node)}"
                )
                lines.extend(str(note) for note in node["notes"])
            updates["exc_text"] = "\n".join(lines)
        updates["args"] = ()
        for name in ("stack_info", "exc_text"):
            if updates.get(name) is not None:
                updates[name] = scrub_text(str(updates[name]), credentials)
        object.__setattr__(record, "__dict__", updates)


def _native_int(value: object) -> object:
    """An int subclass's value (bool, IntEnum) copied without calling its methods."""
    return int.__index__(cast("int", value)) if issubclass(type(value), int) else value


def _install_record_creation() -> None:
    previous_log = logging.Logger.__dict__["_log"]

    def inert_log(
        self: logging.Logger,
        *positional: object,
        **options: object,
    ) -> None:
        # Preserve the stdlib's positional and keyword _log calling convention.
        fields: dict[str, object] = dict(
            zip(
                (
                    "level",
                    "msg",
                    "args",
                    "exc_info",
                    "extra",
                    "stack_info",
                    "stacklevel",
                ),
                positional,
                strict=False,
            )
        )
        fields.update(_plain_storage(options))
        level = _native_int(fields["level"])
        stack_info = fields.get("stack_info", False)
        if stack_info is None or issubclass(type(stack_info), int):
            # The stdlib only tests its truth, which for an int is its value.
            stack_info = stack_info is not None and _native_int(stack_info) != 0
        stacklevel = _native_int(fields.get("stacklevel", 1))
        args = cast("tuple[object, ...]", fields["args"])
        if any(
            type(value) is not expected
            for value, expected in (
                (level, int),
                (stack_info, bool),
                (stacklevel, int),
                (args, tuple),
            )
        ):
            previous_log(
                self,
                logging.ERROR,
                f"{SENTRY_REDACTION_MARKER} log record withheld: redaction failed",
                (),
                stacklevel=2,
            )
            return
        try:
            extra = fields.get("extra")
            exc_info = fields.get("exc_info")
            if (
                len(args) == 1
                and type(args[0]) is not dict
                and issubclass(type(args[0]), dict)
            ):
                args = (_project_mapping(args[0]),)
            safe_args = cast("tuple[Any, ...]", project_structure(args))
            safe_extra = (
                cast("dict[str, Any]", project_structure(_plain_storage(extra)))
                if extra is not None
                else None
            )
            safe_info = None
            if exc_info is not None and exc_info is not False:
                safe_info = _safe_exception_info(exc_info)
            safe_msg = project_structure(fields["msg"])
        except Exception:  # noqa: BLE001 - no original reaches record creation
            safe_msg = (
                f"{SENTRY_REDACTION_MARKER} log record withheld: redaction failed"
            )
            safe_args, safe_extra, safe_info = (), None, None
        previous_log(
            self,
            cast("int", level),
            safe_msg,
            safe_args,
            exc_info=safe_info,
            extra=safe_extra,
            stack_info=cast("bool", stack_info),
            stacklevel=cast("int", stacklevel) + 1,
        )

    type.__setattr__(logging.Logger, "_log", inert_log)


def _install_record_dispatch() -> None:
    previous = logging.Logger.callHandlers

    def call_handlers(self: logging.Logger, record: logging.LogRecord) -> None:
        try:
            boundary = sentry_sdk.get_client().get_integration(
                CredentialLoggingBoundary
            )
        except Exception:  # noqa: BLE001 - fail closed before selecting a client
            return
        if boundary is None and __name__ != settings.SETTINGS_MODULE:
            previous(self, record)
            return
        try:
            if not CredentialURLLogFilter().filter(record):
                return
            previous(self, record)
        except Exception:  # noqa: BLE001 - never expose originals from SDK dispatch
            return

    type.__setattr__(logging.Logger, "callHandlers", call_handlers)


def _safe_capture_hint(hint: Hint | None) -> Hint:
    source = _plain_storage(hint) if hint is not None else {}
    result: Hint = {}
    if "log_record" in source:
        record = source["log_record"]
        if type(record) is not logging.LogRecord:
            raise TypeError
        if _plain_storage(record.__dict__).get(_DROPPED_RECORD) is True:
            raise TypeError
        if not CredentialURLLogFilter().filter(record):
            raise TypeError
        result["log_record"] = record
    if source.get("exc_info") is not None:
        result["exc_info"] = cast("Any", _safe_exception_info(source["exc_info"]))
    return result


def _project_capture_kwargs(values: dict[str, object]) -> dict[str, Any]:
    """Own every keyword before Scope.update_from_kwargs merges its values."""
    return {
        name: (
            _project_mapping(value)
            if name in {"extras", "contexts", "tags", "attributes", "user"}
            and value is not None
            else project_structure(value)
        )
        for name, value in _plain_storage(values).items()
    }


_SCOPE_SLOTS: Final = {
    name: sentry_sdk.Scope.__dict__[name] for name in sentry_sdk.Scope.__slots__
}
# The only scope data a capture may carry, each slot copied as a projection into a
# fresh SDK scope. Every other slot keeps that scope's own empty default, so the
# SDK never reads attachments (raw payloads and callables no scrubber sees),
# feature flags, profiles, gen-AI ids or a live span or session protocol.
_SCOPE_VALUES: Final = ("_level", "_fingerprint", "_transaction")
_SCOPE_MAPPINGS: Final = (
    "_transaction_info",
    "_tags",
    "_contexts",
    "_extras",
    "_attributes",
    "_gen_ai_original_message_count",
)
_SCOPE_TRACING: Final = ("_span", "_propagation_context", "_session")


def _scope_slot(scope: object, name: str) -> object:
    """Read a native Scope slot without any attribute, truth or container hook."""
    if type(scope) is not sentry_sdk.Scope:
        raise TypeError
    return _SCOPE_SLOTS[name].__get__(scope)


def _scope_items(value: object) -> list[object]:
    """A breadcrumb buffer's items, read from native deque or list storage."""
    if value is None:
        return []
    if issubclass(type(value), deque):
        return list(deque.__iter__(cast("deque[object]", value)))
    if issubclass(type(value), list):
        return list.copy(cast("list[object]", value))
    raise TypeError


def _trace_ids(owner: type, value: object) -> tuple[str, str, str | None] | None:
    """Trace and span ids from native slots; the SDK itself fills an empty one."""
    ids: list[str] = []
    for name in ("trace_id", "span_id"):
        stored = owner.__dict__["_" + name].__get__(value)
        if stored is None or (type(stored) is str and not stored):
            stored = owner.__dict__[name].fget(value)
        if type(stored) is not str:
            return None
        ids.append(stored)
    parent = owner.__dict__["parent_span_id"].__get__(value)
    return ids[0], ids[1], parent if type(parent) is str else None


def _owned_baggage(value: object, *, frozen: bool) -> Baggage | None:
    """Dynamic sampling items, copied as projections into a new frozen baggage."""
    if type(value) is not Baggage or (
        frozen and Baggage.__dict__["mutable"].__get__(value) is not False
    ):
        return None
    return Baggage(
        _project_mapping(Baggage.__dict__["sentry_items"].__get__(value)),
        mutable=False,
    )


def _owned_trace(span: object, propagation: object) -> PropagationContext:
    """Trace linkage rebuilt from ids, so the SDK keeps no live tracing object.

    With an active SDK span these are the ids Span.get_trace_context reports, with
    its transaction's frozen sampling baggage; otherwise the propagation context's.
    The SDK derives the trace context and its sampling items from them alone.
    """
    if (type(span) is Span or type(span) is Transaction) and has_tracing_enabled(
        sentry_sdk.get_client().options
    ):
        ids = _trace_ids(Span, span)
        if ids is not None:
            owner = (
                span
                if type(span) is Transaction
                else Span.__dict__["_containing_transaction"].__get__(span)
            )
            baggage = (
                _owned_baggage(
                    Transaction.__dict__["_baggage"].__get__(owner), frozen=True
                )
                if type(owner) is Transaction
                else None
            )
            trace_id, span_id, parent = ids
            return PropagationContext(trace_id, span_id, parent, baggage=baggage)
    if type(propagation) is PropagationContext:
        ids = _trace_ids(PropagationContext, propagation)
        if ids is not None:
            sampled = PropagationContext.__dict__["parent_sampled"].__get__(propagation)
            trace_id, span_id, parent = ids
            return PropagationContext(
                trace_id,
                span_id,
                parent,
                parent_sampled=sampled if type(sampled) is bool else None,
                baggage=_owned_baggage(
                    PropagationContext.__dict__["baggage"].__get__(propagation),
                    frozen=False,
                ),
            )
    return PropagationContext()


def _native_session(value: object) -> bool:
    """Release health may count the error on the SDK's own native session."""
    if type(value) is not Session:
        return False
    state = _plain_storage(object.__getattribute__(value, "__dict__"))
    agent = state.get("user_agent")
    return type(state.get("errors")) is int and (agent is None or type(agent) is str)


def _ambient_scopes() -> list[object]:
    """The global, isolation and current scopes, in the SDK's merge order."""
    return [
        scope
        for scope in (
            sentry_scope._global_scope,  # noqa: SLF001 - the SDK's merge sources
            sentry_scope._isolation_scope.get(),  # noqa: SLF001
            sentry_scope._current_scope.get(),  # noqa: SLF001
        )
        if scope is not None
    ]


def _require_native_processors() -> None:
    """The SDK runs the ambient scopes' processors; the lists must be its own."""
    lists = [
        _scope_slot(scope, name)
        for scope in _ambient_scopes()
        for name in ("_event_processors", "_error_processors")
    ]
    lists.append(sentry_scope.global_event_processors)
    if any(type(value) is not list for value in lists):
        raise TypeError


def _fold_scope(state: dict[str, Any], source: object) -> None:
    """Fold one source in as Scope.update_from_scope would, from native slots."""
    for name in (*_SCOPE_VALUES, "_user", *_SCOPE_TRACING):
        value = _scope_slot(source, name)
        if value is not None:
            state[name] = value
    for name in _SCOPE_MAPPINGS:
        value = _scope_slot(source, name)
        if value is not None:
            dict.update(state[name], _project_mapping(value))
    state["_breadcrumbs"].extend(
        _project_mapping(crumb)
        for crumb in _scope_items(_scope_slot(source, "_breadcrumbs"))
    )
    count = _scope_slot(source, "_n_breadcrumbs_truncated")
    if type(count) is int and count > 0:
        state["_n_breadcrumbs_truncated"] += count


def _owned_scope(
    sources: list[object], keywords: dict[str, object] | None = None
) -> sentry_sdk.Scope:
    """A fresh SDK scope holding only projections of allowlisted source data.

    Sources are read through native Scope slots, never mutated and never handed
    to the SDK. Later sources win, as in Scope.update_from_scope, then keywords,
    as in Scope.update_from_kwargs.
    """
    _require_native_processors()
    state: dict[str, Any] = {name: {} for name in _SCOPE_MAPPINGS}
    state.update(_breadcrumbs=[], _n_breadcrumbs_truncated=0)
    for source in sources:
        _fold_scope(state, source)
    merged = sentry_sdk.Scope(ty=ScopeType.MERGED)
    for name in _SCOPE_VALUES:
        if name in state:
            _SCOPE_SLOTS[name].__set__(merged, project_structure(state[name]))
    if "_user" in state:
        _SCOPE_SLOTS["_user"].__set__(merged, _project_mapping(state["_user"]))
    for name in (*_SCOPE_MAPPINGS, "_n_breadcrumbs_truncated"):
        _SCOPE_SLOTS[name].__set__(merged, state[name])
    _SCOPE_SLOTS["_breadcrumbs"].__set__(merged, deque(state["_breadcrumbs"]))
    _SCOPE_SLOTS["_propagation_context"].__set__(
        merged, _owned_trace(state.get("_span"), state.get("_propagation_context"))
    )
    if _native_session(state.get("_session")):
        _SCOPE_SLOTS["_session"].__set__(merged, state["_session"])
    if keywords:
        merged.update_from_kwargs(**_project_capture_kwargs(keywords))
    return merged


def _refused_scope() -> sentry_sdk.Scope:
    """A merged scope the SDK will not capture with or apply data from."""
    refused = sentry_sdk.Scope(ty=ScopeType.MERGED)
    object.__setattr__(refused, "_should_capture", False)
    return refused


def _native_copy(value: object) -> object:
    """Copy a native container natively; every other value is shared, as fork()."""
    if type(value) is dict:
        return dict.copy(value)
    if type(value) is list:
        return list.copy(value)
    if type(value) is deque:
        return deque.copy(value)
    return value


def _capture_scope(scope: object) -> sentry_sdk.Scope:
    """Own a scope handed to the client; one the merge refused stays refused."""
    if _scope_slot(scope, "_should_capture") is not True:
        raise TypeError
    return _owned_scope([scope])


def _install_scope_merge() -> None:
    previous = sentry_sdk.Scope._merge_scopes  # noqa: SLF001 - every capture merges here
    previous_copy = sentry_sdk.Scope.__copy__

    def merge_scopes(
        self: sentry_sdk.Scope,
        additional_scope: sentry_sdk.Scope | None = None,
        additional_scope_kwargs: dict[str, Any] | None = None,
    ) -> sentry_sdk.Scope:
        if sentry_sdk.get_client().get_integration(CredentialLoggingBoundary) is None:
            return previous(self, additional_scope, additional_scope_kwargs)
        # The SDK drops events and skips scope data for a non-capturing scope, and
        # the client boundaries refuse it, so no caller sees an error.
        if additional_scope is not None and additional_scope_kwargs:
            return _refused_scope()
        try:
            isolation = sentry_scope._isolation_scope.get()  # noqa: SLF001
            current = sentry_scope._current_scope.get()  # noqa: SLF001
            sources = _ambient_scopes()
            if self is not current and self is not isolation:
                sources.append(self)
            if additional_scope is not None:
                sources.append(additional_scope)
            return _owned_scope(sources, additional_scope_kwargs)
        except Exception:  # noqa: BLE001 - an unowned source refuses the capture
            return _refused_scope()

    def copy_scope(self: sentry_sdk.Scope) -> sentry_sdk.Scope:
        if sentry_sdk.get_client().get_integration(CredentialLoggingBoundary) is None:
            return previous_copy(self)
        # The SDK forks the ambient scopes whenever a thread starts, including its
        # own batcher and transport threads inside a capture. Copy native storage
        # only: a replaced container is shared, never asked to copy itself.
        forked = object.__new__(type(self))
        for slot in _SCOPE_SLOTS.values():
            slot.__set__(forked, _native_copy(slot.__get__(self)))
        # Feature flags never reach a capture, so a fork starts without them
        # rather than sharing or deep-copying application flag values.
        _SCOPE_SLOTS["_flags"].__set__(forked, None)
        return forked

    type.__setattr__(sentry_sdk.Scope, "_merge_scopes", merge_scopes)
    type.__setattr__(sentry_sdk.Scope, "__copy__", copy_scope)


def _install_capture_boundary() -> None:
    previous_capture = sentry_sdk.Client.capture_event
    previous_scoped_capture = sentry_sdk.Scope.capture_event

    def capture_event(
        self: sentry_sdk.Client,
        event: Event,
        hint: Hint | None = None,
        scope: sentry_sdk.Scope | None = None,
    ) -> str | None:
        if self.get_integration(CredentialLoggingBoundary) is None:
            return previous_capture(self, event, hint=hint, scope=scope)
        try:
            owned = _own_event_carrier(event)
            if owned is None:
                return None
            return previous_capture(
                self,
                owned,
                hint=_safe_capture_hint(hint),
                scope=None if scope is None else _capture_scope(scope),
            )
        except Exception:  # noqa: BLE001 - production telemetry fails closed
            return None

    def scoped_capture_event(
        self: sentry_sdk.Scope,
        event: Event,
        hint: Hint | None = None,
        scope: sentry_sdk.Scope | None = None,
        **scope_kwargs: object,
    ) -> str | None:
        if self.get_client().get_integration(CredentialLoggingBoundary) is None:
            return previous_scoped_capture(
                self, event, hint=hint, scope=scope, **scope_kwargs
            )
        try:
            owned = _own_event_carrier(event)
            if owned is None:
                return None
            return previous_scoped_capture(
                self,
                owned,
                hint=_safe_capture_hint(hint),
                scope=None if scope is None else _owned_scope([scope]),
                **_project_capture_kwargs(scope_kwargs),
            )
        except Exception:  # noqa: BLE001 - includes the SDK's post-capture read
            return None

    # Check-ins skip event processors, and scope-less calls skip scopes.
    # Own carriers at both capture entry points, before the SDK can call any
    # application accessor or serialize a supposedly irrevocable husk.
    type.__setattr__(sentry_sdk.Client, "capture_event", capture_event)
    type.__setattr__(sentry_sdk.Scope, "capture_event", scoped_capture_event)


class CredentialLoggingBoundary(Integration):
    """Guard the SDK's outer dispatch and give every event hook a plain dict."""

    identifier = "credential_logging_boundary"

    @staticmethod
    def setup_once() -> None:
        _install_capture_boundary()
        _install_scope_merge()
        _install_record_dispatch()
        _install_exception_capture()
        _install_sdk_carriers()
        _install_sdk_stack_projection()


def _own_event_carrier(event: Event) -> Event | None:
    """Continue with a plain dict the hooks own, or refuse the carrier.

    The scrubber hook rewrites through unbound dict primitives, but the SDK then
    serializes through the carrier's own accessors: a dict subclass could revive
    a husk, and a mapping that is not a dict cannot be rewritten at all. Reading
    a subclass's storage through dict.items calls none of its methods; the
    original keeps only the withheld marker.
    """
    carrier = cast("object", event)
    if not issubclass(type(carrier), dict):
        return None
    original = cast("dict[str, object]", carrier)
    source = _plain_storage(original)
    if source.get(_WITHHELD_EVENT) is True:
        return None
    if type(carrier) is dict:
        # The public cron API reads check_in_id after capture returns. A failed
        # scrub must husk only our copy, not destroy that caller's return value.
        event_type = source.get("type")
        if type(event_type) is str and event_type == "check_in":
            return cast("Event", project_structure(dict.copy(original)))
        # Freeze objects now, but discover credentials only after scope merging.
        # Early text redaction can destroy a range crossing a later scope secret.
        owned = cast("dict[str, Any]", project_structure(source))
        dict.clear(original)
        dict.update(original, owned)
        return cast("Event", cast("object", original))
    owned = source
    dict.clear(original)
    dict.update(original, {_WITHHELD_EVENT: True})
    return cast("Event", project_structure(owned))


def _install_exception_capture() -> None:
    previous = sentry_sdk.Scope.capture_exception

    def capture_exception(
        self: sentry_sdk.Scope,
        error: object = None,
        scope: sentry_sdk.Scope | None = None,
        **scope_kwargs: object,
    ) -> str | None:
        if self.get_client().get_integration(CredentialLoggingBoundary) is None:
            return previous(self, cast("Any", error), scope=scope, **scope_kwargs)
        try:
            return previous(
                self,
                cast("Any", _safe_exception_info(error)),
                scope=None if scope is None else _owned_scope([scope]),
                **_project_capture_kwargs(scope_kwargs),
            )
        except Exception:  # noqa: BLE001 - never render a failing capture
            return None

    type.__setattr__(sentry_sdk.Scope, "capture_exception", capture_exception)
    _install_sdk_exception_conversion()


def _install_sdk_exception_conversion() -> None:
    previous_values = sentry_utils.exceptions_from_error_tuple
    previous_task = sentry_celery._capture_exception  # noqa: SLF001 - SDK ingress

    def exceptions_from_error_tuple(
        exc_info: _ExceptionInfo,
        client_options: dict[str, Any] | None = None,
        mechanism: dict[str, Any] | None = None,
        full_stack: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if (
            client_options is not None
            and client_options.get("before_send") is drop_unscrubbable_event
        ):
            return _exception_values(exc_info, mechanism)
        return previous_values(
            exc_info, cast("Any", client_options), mechanism, full_stack
        )

    def capture_task_exception(
        task: object,
        exc_info: _ExceptionInfo,
    ) -> None:
        if sentry_sdk.get_client().get_integration(CredentialLoggingBoundary) is None:
            previous_task(task, exc_info)
            return
        try:
            # The SDK checks control-flow types before its conversion helper.
            # This is Sentry's ingress, not a hook into Celery's P3 renderer.
            ancestry = type.__dict__["__mro__"].__get__(type(exc_info[1]))
            if any(
                base is control
                for base in ancestry
                for control in sentry_celery.CELERY_CONTROL_FLOW_EXCEPTIONS
            ):
                sentry_celery._set_status("aborted")  # noqa: SLF001
                return
            sentry_celery._set_status("internal_error")  # noqa: SLF001
            if _task_declares_exception(task, ancestry):
                return
            event, hint = sentry_utils.event_from_exception(
                cast("Any", _safe_exception_info(exc_info)),
                client_options=sentry_sdk.get_client().options,
                mechanism={"type": "celery", "handled": False},
            )
            sentry_sdk.capture_event(event, hint=hint)
        except Exception:  # noqa: BLE001 - telemetry must not replace a task failure
            return

    sentry_utils.exceptions_from_error_tuple = exceptions_from_error_tuple
    sentry_celery._capture_exception = capture_task_exception  # noqa: SLF001


def _task_declares_exception(task: object, ancestry: tuple[type, ...]) -> bool:
    for kind in type.__dict__["__mro__"].__get__(type(task)):
        namespace = type.__dict__["__dict__"].__get__(kind)
        for name, declared in namespace.items():
            if type(name) is str and name == "throws":
                choices = declared if type(declared) is tuple else (declared,)
                return any(
                    base is expected for base in ancestry for expected in choices
                )
    return False


def _install_sdk_logger_projection() -> None:
    previous = sdk_logger._capture_log  # noqa: SLF001 - before public SDK formatting
    previous_attribute = sentry_sdk.Scope.set_attribute

    def capture_log(
        severity_text: str,
        severity_number: int,
        template: object,
        **kwargs: object,
    ) -> None:
        try:
            source = _plain_storage(kwargs)
            if source.get("attributes") is not None:
                source["attributes"] = _project_mapping(source["attributes"])
            owned = cast("dict[str, Any]", project_structure(source))
            message = str(project_structure(template))
        except Exception:  # noqa: BLE001 - no object reaches SDK format_map/repr
            return
        previous(severity_text, severity_number, message, **owned)

    def set_attribute(self: sentry_sdk.Scope, attribute: str, value: object) -> None:
        try:
            name = str(project_structure(attribute))
            owned = project_structure(value)
        except Exception:  # noqa: BLE001 - unsupported attribute carriers are refused
            return
        previous_attribute(self, name, cast("Any", owned))

    sdk_logger._capture_log = capture_log  # noqa: SLF001
    for name, severity, number in (
        ("trace", "trace", 1),
        ("debug", "debug", 5),
        ("info", "info", 9),
        ("warning", "warn", 13),
        ("error", "error", 17),
        ("fatal", "fatal", 21),
    ):
        setattr(sdk_logger, name, partial(capture_log, severity, number))
    type.__setattr__(sentry_sdk.Scope, "set_attribute", set_attribute)


def _install_sdk_carriers() -> None:
    previous_log = sentry_sdk.Client._capture_log  # noqa: SLF001 - before scope merging
    previous_breadcrumb = sentry_sdk.Scope.add_breadcrumb

    def capture_log(
        self: sentry_sdk.Client, log: Log | None, scope: sentry_sdk.Scope
    ) -> None:
        if self.get_integration(CredentialLoggingBoundary) is None:
            previous_log(self, log, scope)
            return
        try:
            if log is None:
                return
            source = _plain_storage(log)
            source["attributes"] = _project_mapping(source.get("attributes", {}))
            owned_scope = _capture_scope(scope)
            previous_log(self, cast("Log", project_structure(source)), owned_scope)
        except Exception:  # noqa: BLE001 - no carrier enters SDK telemetry rendering
            return

    def add_breadcrumb(
        self: sentry_sdk.Scope,
        crumb: Breadcrumb | None = None,
        hint: BreadcrumbHint | None = None,
        **kwargs: object,
    ) -> None:
        if self.get_client().get_integration(CredentialLoggingBoundary) is None:
            previous_breadcrumb(self, crumb, hint, **kwargs)
            return
        try:
            # The SDK appends, measures and counts through these two containers.
            if (
                type(_scope_slot(self, "_breadcrumbs")) is not deque
                or type(_scope_slot(self, "_n_breadcrumbs_truncated")) is not int
            ):
                return
            source = _plain_storage(crumb) if crumb is not None else {}
            source.update(_plain_storage(kwargs))
            previous_breadcrumb(
                self,
                cast("Breadcrumb", project_structure(source)),
                _safe_capture_hint(hint),
            )
        except Exception:  # noqa: BLE001 - before dict(crumb), bool and accessor calls
            return

    type.__setattr__(sentry_sdk.Client, "_capture_log", capture_log)
    type.__setattr__(sentry_sdk.Scope, "add_breadcrumb", add_breadcrumb)


def _install_sdk_stack_projection() -> None:
    previous = cast(
        "Callable[..., dict[str, Any]]",
        sentry_logging.__dict__["current_stacktrace"],
    )
    emit_code = sentry_logging.EventHandler.__dict__["_emit"].__code__

    def current_stacktrace(*args: object, **kwargs: object) -> dict[str, Any]:
        if sentry_sdk.get_client().get_integration(CredentialLoggingBoundary) is None:
            return previous(*args, **kwargs)
        frame: FrameType | None = sys._getframe(1)  # noqa: SLF001 - SDK frame only
        while frame is not None:
            if frame.f_code is emit_code:
                # This is the SDK's own argument, not application frame locals.
                # Its handlers already applied the production ingress filter.
                record = frame.f_locals.get("record")
                if type(record) is not logging.LogRecord:
                    break
                rows = project_structure(
                    _plain_storage(record.__dict__).get(_RECORD_STACK, [])
                )
                return {
                    "frames": [
                        {"filename": row[0], "function": row[1], "lineno": row[2]}
                        for row in cast("list[list[Any]]", rows)
                    ]
                }
            frame = frame.f_back
        return {"frames": []}

    sentry_logging.__dict__["current_stacktrace"] = current_stacktrace


# Record construction precedes every console/SDK formatter, including workers
# without a DSN. Integration setup adds an outer dispatch guard after the SDK's
# own callHandlers patch has been installed.
_install_record_creation()
_install_record_dispatch()
_install_sdk_logger_projection()


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
