"""A failed Celery task must not break, or over-redact, the production log paths."""

import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
import sentry_sdk
from celery import Celery
from celery.result import EagerResult

from apps.delivery.privacy import delivery_secrets, sensitive_values
from config.settings import prod
from tests.security.test_credential_log_hygiene import _CaptureTransport

ARG_CANARY = "synthetic-task-arg-canary-5521"
KWARG_CANARY = "synthetic-task-kwarg-canary-5521"
EXC_CANARY = "synthetic-task-exception-canary-5521"
BODY_CANARY = "synthetic-task-body-canary-5521"
RESPONSE_CANARY = "synthetic-provider-response-canary-5521"
TASK_NAME = "witness.provider_call"
TASK_ID = "8f1c0b6e-5f2a-4e1d-9a52-3d1b7a4c0e55"
CANARIES = (ARG_CANARY, KWARG_CANARY, EXC_CANARY, BODY_CANARY)
MARKER = prod.SENTRY_REDACTION_MARKER

# Not the current app: a private app cannot leak into the project's Celery routing.
witness_app = Celery("axrm011-task-failure-witness", set_as_current=False)
witness_app.conf.task_always_eager = True
witness_app.conf.task_store_eager_result = False


def _provider_call(secret: str, short: str, *, note: str, body: dict[str, str]) -> None:
    logging.getLogger("axrm011.witness").warning("calling provider with %s", secret)
    message = (
        f"provider rejected {secret} {note} {body['payload']} {EXC_CANARY} "
        f"{RESPONSE_CANARY}"
    )
    raise RuntimeError(message)


provider_call = witness_app.task(name=TASK_NAME)(_provider_call)


@contextmanager
def _failure_logger(
    monkeypatch: pytest.MonkeyPatch, handler: logging.Handler
) -> Iterator[None]:
    logger = logging.getLogger("celery.app.trace")
    # Isolated from whatever the process-wide root handlers carry.
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()


class _MessageOnly(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def _production_filter() -> logging.Filter:
    # Exactly as prod installs it: the class named by the console handler's filter.
    assert prod.LOGGING["handlers"]["console"]["filters"] == ["credential_urls"]
    filter_class = cast(
        "type[logging.Filter]", prod.LOGGING["filters"]["credential_urls"]["()"]
    )
    return filter_class()


def _run_task() -> EagerResult:
    return cast(
        "EagerResult",
        provider_call.apply(
            args=[ARG_CANARY, "ex"],
            kwargs={"note": KWARG_CANARY, "body": {"payload": BODY_CANARY}},
            task_id=TASK_ID,
        ),
    )


def _fail_eagerly() -> EagerResult:
    # The exception also names a secret only the delivery context knows.
    with delivery_secrets(EXC_CANARY):
        return _run_task()


def test_a_failed_task_logs_through_the_production_console_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the production console filter on Celery's task-failure logger.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_MessageOnly())
    handler.addFilter(_production_filter())
    with _failure_logger(monkeypatch, handler):
        # When a task fails eagerly; a filter that raises would escape from apply().
        result = _fail_eagerly()
    # Then the message names the task and its id, and none of what it carried or
    # raised, including text no one knew to remove (the traceback is not covered).
    output = stream.getvalue()
    assert result.state == "FAILURE"
    assert TASK_NAME in output
    assert TASK_ID in output
    assert MARKER in output
    assert all(canary not in output for canary in (*CANARIES, RESPONSE_CANARY))


def test_a_failed_tasks_sentry_event_drops_the_secrets_in_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real Sentry client with the production options. Its Celery
    # integration ignores the trace logger and withholds the task's arguments,
    # so the exception event and its breadcrumbs are what carry them.
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        **{
            **prod.SENTRY_OPTIONS,
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
        }
    )
    monkeypatch.setattr(logging.getLogger("celery.app.trace"), "propagate", False)
    try:
        with (
            sentry_sdk.isolation_scope() as scope,
            delivery_secrets(EXC_CANARY),
        ):
            scope.set_client(client)
            # When the task fails and the event passes through before_send.
            _run_task()
            known_after_the_task = sensitive_values.get()
            client.flush()
    finally:
        client.close()
    # Then the event names the task, keeps its own field names and carries no secret.
    serialized = json.dumps(transport.events)
    assert transport.events
    assert TASK_NAME in serialized
    assert TASK_ID in serialized
    assert MARKER in serialized
    assert all(canary not in serialized for canary in CANARIES)
    assert '"vars"' not in serialized
    assert any(
        "calling provider" in json.dumps(e.get("breadcrumbs")) for e in transport.events
    )
    assert all(e.get("exception", {}).get("values") for e in transport.events)
    assert known_after_the_task == (EXC_CANARY,)


def test_extras_named_like_the_filters_bookkeeping_survive_scrubbing() -> None:
    # Given a hidden payload whose key "exc" is a substring of bookkeeping names.
    exception = RuntimeError(EXC_CANARY)
    record = logging.makeLogRecord(
        {
            "levelno": logging.WARNING,
            "levelname": "WARNING",
            "msg": "diagnostic %s",
            "args": (ARG_CANARY,),
            "exc_info": (RuntimeError, exception, None),
            "payload": {"exc": KWARG_CANARY},
            "exception_arguments": ["kept-unchanged"],
        }
    )
    # When the production filter scrubs the record.
    with delivery_secrets(ARG_CANARY, EXC_CANARY):
        assert _production_filter().filter(record) is True
    # Then no field was renamed or lost, and the secrets are gone.
    assert record.__dict__["exception_arguments"] == ["kept-unchanged"]
    assert record.levelname == "WARNING"
    assert record.exc_info is not None
    rendered = f"{record.getMessage()} {record.exc_text} {record.exc_info[1]}"
    assert all(canary not in rendered for canary in CANARIES)
    assert MARKER in rendered
    assert exception.args == (EXC_CANARY,)


def test_task_context_from_another_logger_stays_hidden_whole() -> None:
    # Given the same context shape from a logger other than Celery's.
    context = {
        "hostname": "worker",
        "id": TASK_ID,
        "name": TASK_NAME,
        "exc": EXC_CANARY,
        "traceback": "Traceback",
        "args": f"[{ARG_CANARY!r}]",
        "kwargs": "{}",
        "description": "raised unexpected",
        "internal": False,
    }
    record = logging.makeLogRecord(
        {
            "name": "not.celery",
            "msg": "Task %(name)s[%(id)s] %(description)s: %(exc)s",
            "args": (context,),
            "data": context,
        }
    )
    # When the production filter scrubs it.
    assert _production_filter().filter(record) is True
    # Then an unrecognized producer keeps the fail-closed treatment of provider data.
    assert TASK_NAME not in record.getMessage()
    assert EXC_CANARY not in record.getMessage()


def _wrong_shape(pairs: list[Any], *_args: object, **_kwargs: object) -> object:
    return pairs[:-1]


def _not_pairs(pairs: list[Any], *_args: object, **_kwargs: object) -> object:
    return [None] * len(pairs)


def _extra_field(pairs: list[Any], *_args: object, **_kwargs: object) -> object:
    return [*pairs, ["unexpected", 1]]


def _explodes(*_args: object, **_kwargs: object) -> object:
    message = f"redaction failed on {EXC_CANARY}"
    raise KeyError(message)


WITHHELD_CANARY = "synthetic-withheld-field-canary-7003"


class _StrictConsole(logging.StreamHandler[io.StringIO]):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - override
        message = "the withheld record could not be formatted"
        raise AssertionError(message)


def _production_console() -> tuple[logging.Handler, io.StringIO]:
    stream = io.StringIO()
    handler = _StrictConsole(stream)
    handler.setFormatter(
        logging.Formatter(prod.LOGGING["formatters"]["simple"]["format"])
    )
    for name in prod.LOGGING["handlers"]["console"]["filters"]:
        handler.addFilter(prod.LOGGING["filters"][name]["()"]())
    return handler, stream


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "funcName",
        "threadName",
        "processName",
        "created",
    ],
)
def test_a_withheld_record_emits_no_original_field_through_the_production_console(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Given the configured production handler and formatter, and a record whose
    # standard field, message and extra all carry a credential.
    handler, stream = _production_console()
    record = logging.makeLogRecord(
        {
            "msg": WITHHELD_CANARY,
            "private_extra": WITHHELD_CANARY,
            field: WITHHELD_CANARY,
        }
    )
    # When scrubbing fails and the record is withheld.
    monkeypatch.setattr(prod, "redact_structure", _explodes)
    handler.handle(record)
    # Then the record is still formatted, and nothing of the original is emitted.
    emitted = stream.getvalue()
    assert MARKER in emitted
    assert WITHHELD_CANARY not in emitted
    assert WITHHELD_CANARY not in repr(record.__dict__)


@pytest.mark.parametrize(
    "broken",
    [_wrong_shape, _extra_field, _not_pairs, _explodes],
    ids=["missing-field", "extra-field", "unexpected-shape", "redactor-raises"],
)
def test_a_record_the_filter_cannot_scrub_is_withheld_not_raised(
    monkeypatch: pytest.MonkeyPatch, broken: Callable[..., object]
) -> None:
    # Given a record with secrets in its message, extras and exception.
    record = logging.makeLogRecord(
        {
            "msg": "diagnostic %s",
            "args": (ARG_CANARY,),
            "exc_info": (RuntimeError, RuntimeError(EXC_CANARY), None),
            "diagnostic": KWARG_CANARY,
        }
    )
    # When the redactor misbehaves in the middle of filtering.
    monkeypatch.setattr(prod, "redact_structure", broken)
    # Then the filter does not raise and nothing of the record survives but a marker.
    assert prod.CredentialURLLogFilter().filter(record) is True
    assert record.getMessage().startswith(MARKER)
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert not hasattr(record, "diagnostic")
    assert all(canary not in record.getMessage() for canary in CANARIES)
