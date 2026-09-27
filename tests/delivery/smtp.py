"""A real aiosmtpd peer with event-driven, bounded thread lifecycle."""

import asyncio
import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from aiosmtpd.smtp import SMTP, Envelope, Session


@dataclass
class FakeSMTP:
    mode: str = "accept"
    received: list[bytes] = field(default_factory=list)
    rcpt_seen: threading.Event = field(default_factory=threading.Event)
    ready: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    port: int = 0
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    errors: list[BaseException] = field(default_factory=list)
    thread: threading.Thread | None = None

    async def handle_RCPT(  # noqa: N802 - aiosmtpd callback protocol
        self,
        server: SMTP,
        session: Session,
        envelope: Envelope,
        address: str,
        options: list[str],
    ) -> str:
        self.rcpt_seen.set()
        if self.mode == "rcpt_reject":
            return "550 refused"
        envelope.rcpt_tos.append(address)
        return "250 recipient accepted"

    async def handle_DATA(  # noqa: N802 - aiosmtpd callback protocol
        self,
        server: SMTP,
        session: Session,
        envelope: Envelope,
    ) -> str:
        assert envelope.original_content is not None
        self.received.append(envelope.original_content)
        if self.mode == "hang":
            # The SMTP client's configured socket timeout is the behavior under test.
            await asyncio.Future[None]()
        if self.mode == "disconnect":
            assert server.transport is not None
            server.transport.close()
            return "250 unused"
        if self.mode == "temporary":
            return "451 retry later"
        if self.mode == "reject":
            return "550 DATA refused"
        return "250 queued by fake peer"

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            server = self.loop.run_until_complete(
                self.loop.create_server(lambda: SMTP(self), "127.0.0.1", 0)
            )
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            self.loop.run_forever()
            server.close()
            self.loop.run_until_complete(server.wait_closed())
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        except BaseException as error:  # noqa: BLE001 - propagated to test thread
            self.errors.append(error)
            self.ready.set()
        finally:
            self.loop.close()
            self.stopped.set()

    def __enter__(self) -> Self:
        self.thread = threading.Thread(
            target=self._run, name="t10-fake-smtp", daemon=True
        )
        self.thread.start()
        assert self.ready.wait(10), "fake SMTP did not start"
        if self.errors:
            raise self.errors[0]
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        assert self.stopped.wait(10), "fake SMTP did not stop"
        assert self.thread is not None
        self.thread.join(10)
        assert not self.thread.is_alive()
        if self.errors:
            raise self.errors[0]
