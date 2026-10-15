"""Receipt publication must not retain a result subscription nobody consumes."""

import gc
from contextlib import ExitStack
from uuid import uuid4

import pytest
from celery import Celery
from django.conf import settings

from apps.obligations.tasks import materialize_receipt_observations


@pytest.mark.parametrize("keep_result", [False, True], ids=["discarded", "requested"])
def test_receipt_dispatch_only_subscribes_when_a_result_is_requested(
    keep_result: bool,
) -> None:
    # A real, isolated result backend and an in-process broker keep the outgoing
    # message observable without scheduling work on a shared worker queue.
    queue = f"receipt-dispatch-{uuid4().hex}"

    with (
        Celery(
            "receipt-dispatch",
            broker="memory://",
            backend=settings.CELERY_RESULT_BACKEND,
            set_as_current=False,
        ) as app,
        ExitStack() as resources,
        app.connection_for_write() as broker,
    ):
        task = app.tasks[materialize_receipt_observations.name]
        backend = app.backend
        resources.callback(backend.client.connection_pool.disconnect)
        resources.callback(backend.result_consumer.stop)
        with broker.SimpleQueue(queue) as mailbox:
            options = {"ignore_result": False} if keep_result else {}
            result = task.apply_async(connection=broker, queue=queue, **options)
            try:
                message = mailbox.get(block=False)
                message.ack()

                assert message.headers["id"] == result.id
                assert message.headers["task"] == task.name
                expected = (
                    {backend.get_key_for_task(result.id)} if keep_result else set()
                )
                assert backend.result_consumer.subscribed_to == expected
                assert message.headers["ignore_result"] is (not keep_result)
            finally:
                result.forget()
                del result
                # AsyncResult's callback cycle must finalize before its owned
                # result backend closes, or finalization can reconnect it.
                gc.collect()
