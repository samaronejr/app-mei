"""Observe actual lock contention without deciding the writer's authorization."""

from dataclasses import dataclass
from threading import Event, Lock, get_ident
from typing import Self


@dataclass
class ScopeLockProbe:
    lock: Lock
    owner: int
    exit_progress: Event
    exit_waiting: Event
    before_writer_lock: Event | None = None
    allow_writer_lock: Event | None = None

    def __enter__(self) -> Self:
        if get_ident() == self.owner:
            if not self.lock.acquire(blocking=False):
                self.exit_waiting.set()
                self.exit_progress.set()
                self.lock.acquire()
        else:
            if self.before_writer_lock is not None:
                self.before_writer_lock.set()
                assert self.allow_writer_lock is not None
                assert self.allow_writer_lock.wait(timeout=10)
            self.lock.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.lock.release()
