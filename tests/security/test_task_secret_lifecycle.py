"""A running task's argument values are known for that run only, never longer."""

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Protocol

import pytest
from celery import Celery
from celery.signals import task_postrun

from apps.delivery.privacy import delivery_secrets
from config.settings import prod

WAIT_SECONDS = 15

witness_app = Celery("axrm011-task-lifecycle-witness", set_as_current=False)
witness_app.conf.task_always_eager = True
witness_app.conf.task_store_eager_result = False

observed: dict[str, Any] = {}
gates: dict[str, threading.Event] = {}
secrets_by_label: dict[str, str] = {}
stale_probes: list[str] = []


def is_known(secret: str) -> bool:
    event = prod.scrub_sentry_event({"message": f"value {secret}"}, {})
    return secret not in json.dumps(event)


class _BoundTask(Protocol):
    request: Any

    def retry(self, *, countdown: int, max_retries: int) -> Exception: ...


class _Applicable(Protocol):
    def apply(self, *, args: list[str], task_id: str) -> object: ...


@contextmanager
def _without_postrun_signal() -> Iterator[None]:
    saved = list(task_postrun.receivers)
    task_postrun.receivers[:] = []
    task_postrun.sender_receivers_cache.clear()
    try:
        yield
    finally:
        task_postrun.receivers[:] = saved
        task_postrun.sender_receivers_cache.clear()


def _observe(secret: str) -> None:
    observed.setdefault(f"during:{secret}", []).append(is_known(secret))
    observed[f"stale:{secret}"] = [is_known(probe) for probe in stale_probes]


def _succeed(secret: str) -> bool:
    _observe(secret)
    return True


def _fail(secret: str) -> None:
    _observe(secret)
    message = "provider rejected the call"
    raise RuntimeError(message)


def _retry(self: _BoundTask, secret: str) -> bool:
    _observe(secret)
    if self.request.retries == 0:
        raise self.retry(countdown=0, max_retries=1)
    return True


def _overlap(secret: str, label: str) -> bool:
    other = secrets_by_label["B" if label == "A" else "A"]
    observed[f"{label}:during"] = (is_known(secret), is_known(other))
    if label == "A":
        gates["a_running"].set()
        assert gates["b_running"].wait(WAIT_SECONDS)
    else:
        gates["b_running"].set()
        assert gates["a_finished"].wait(WAIT_SECONDS)
    return True


def _nested_scopes(secret: str, inner_scope: str) -> bool:
    with delivery_secrets(inner_scope):
        observed["nested"] = (is_known(secret), is_known(inner_scope))
    observed["restored"] = (is_known(secret), is_known(secrets_by_label["inherited"]))
    return True


def _inner_task(secret: str) -> bool:
    observed.setdefault("inner", []).append(
        (is_known(secrets_by_label["outer"]), is_known(secret))
    )
    return True


succeed = witness_app.task(name="witness.lifecycle.succeed")(_succeed)
fail = witness_app.task(name="witness.lifecycle.fail")(_fail)
retry = witness_app.task(name="witness.lifecycle.retry", bind=True)(_retry)
overlap = witness_app.task(name="witness.lifecycle.overlap")(_overlap)
nested_scopes = witness_app.task(name="witness.lifecycle.scopes")(_nested_scopes)
inner_task = witness_app.task(name="witness.lifecycle.inner")(_inner_task)


def _outer_task(secret: str) -> bool:
    inner_task.apply(args=[secrets_by_label["inner"]])
    observed["between"] = (is_known(secret), is_known(secrets_by_label["inner"]))
    inner_task(secrets_by_label["inner"])
    return True


outer_task = witness_app.task(name="witness.lifecycle.outer")(_outer_task)


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    observed.clear()
    gates.clear()
    secrets_by_label.clear()
    stale_probes.clear()


@pytest.mark.parametrize(
    ("kind", "task"),
    [("success", succeed), ("failure", fail), ("retry", retry)],
)
def test_argument_values_are_known_during_a_run_and_forgotten_after(
    kind: str, task: _Applicable
) -> None:
    # Given a secret that is not known before the run.
    secret = f"synthetic-lifecycle-{kind}-canary-7003"
    assert not is_known(secret)
    # When a task receiving it runs to the end by the way named.
    task.apply(args=[secret], task_id=f"lifecycle-{kind}-7003")
    # Then it was known throughout and is forgotten afterwards.
    during = observed[f"during:{secret}"]
    assert during
    assert all(during)
    assert not is_known(secret)


def test_a_missed_postrun_signal_leaves_nothing_for_the_next_task() -> None:
    # Given a run whose postrun signal is never delivered.
    first = "synthetic-lifecycle-missed-first-canary-7003"
    second = "synthetic-lifecycle-missed-second-canary-7003"
    with _without_postrun_signal():
        succeed.apply(args=[first], task_id="lifecycle-missed-first-7003")
    assert not is_known(first)
    # When an unrelated task runs next.
    stale_probes.append(first)
    succeed.apply(args=[second], task_id="lifecycle-missed-second-7003")
    # Then it neither inherits nor restores the first run's values.
    assert observed[f"during:{second}"] == [True]
    assert observed[f"stale:{second}"] == [False]
    assert not is_known(first)
    assert not is_known(second)


@pytest.mark.parametrize("shared_id", [False, True], ids=["distinct-ids", "one-id"])
def test_overlapping_executions_each_own_their_own_state(shared_id: bool) -> None:
    # Given two executions, one of which finishes while the other still runs.
    secrets_by_label["A"] = "synthetic-lifecycle-overlap-a-canary-7003"
    secrets_by_label["B"] = "synthetic-lifecycle-overlap-b-canary-7003"
    for name in ("a_running", "b_running", "a_finished"):
        gates[name] = threading.Event()

    def run(label: str) -> None:
        if label == "B":
            assert gates["a_running"].wait(WAIT_SECONDS)
        task_id = "lifecycle-overlap-7003" if shared_id else f"lifecycle-{label}-7003"
        result = overlap.apply(args=[secrets_by_label[label], label], task_id=task_id)
        observed[f"{label}:state"] = result.state
        observed[f"{label}:after"] = (
            is_known(secrets_by_label["A"]),
            is_known(secrets_by_label["B"]),
        )
        if label == "A":
            gates["a_finished"].set()

    # When they run on separate threads, sharing a task id or not.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, label) for label in ("A", "B")]
        for future in futures:
            future.result(timeout=2 * WAIT_SECONDS)
    # Then each knew only its own values and each left nothing behind.
    assert observed["A:state"] == observed["B:state"] == "SUCCESS"
    assert observed["A:during"] == (True, False)
    assert observed["B:during"] == (True, False)
    assert observed["A:after"] == (False, False)
    assert observed["B:after"] == (False, False)


def test_inherited_delivery_secrets_survive_and_compose_with_a_task() -> None:
    # Given an inherited redaction context around a task that opens its own scope.
    argument = "synthetic-lifecycle-argument-canary-7003"
    secrets_by_label["inherited"] = "synthetic-lifecycle-inherited-canary-7003"
    inner_scope = "synthetic-lifecycle-inner-scope-canary-7003"
    # When the task runs inside both.
    with delivery_secrets(secrets_by_label["inherited"]):
        nested_scopes.apply(args=[argument, inner_scope])
        after = (is_known(argument), is_known(secrets_by_label["inherited"]))
    # Then the task's values and the nested scope's compose, and each is restored.
    assert observed["nested"] == (True, True)
    assert observed["restored"] == (True, True)
    assert after == (False, True)


def test_nested_tasks_compose_and_each_run_restores_its_own_state() -> None:
    # Given a task that runs another, by apply and by a direct call.
    secrets_by_label["outer"] = "synthetic-lifecycle-outer-canary-7003"
    secrets_by_label["inner"] = "synthetic-lifecycle-inner-canary-7003"
    # When the outer task runs.
    outer_task.apply(args=[secrets_by_label["outer"]], task_id="lifecycle-outer-7003")
    # Then the inner runs saw both, the outer kept only its own, and nothing remains.
    assert observed["inner"] == [(True, True), (True, True)]
    assert observed["between"] == (True, False)
    assert not is_known(secrets_by_label["outer"])
    assert not is_known(secrets_by_label["inner"])
