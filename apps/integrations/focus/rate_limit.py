"""Client-side sliding-window limit: 60 Focus requests per minute per token.

Focus allows 100 requests per minute per token and answers 429 beyond that; Axorim
stays at 60 so a burst never reaches the provider's limit. The window is exact (a
log of send times), keyed by the token's *name* (environment and secret reference),
never by the token value. A refused request is not recorded, so it does not extend
the wait. The log lives in process memory only.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Final

FOCUS_REQUESTS_PER_MINUTE: Final = 60
WINDOW_SECONDS: Final = 60.0


class TokenRateLimiter:
    """Admit at most `limit` sends per `window` seconds for each token key."""

    def __init__(
        self,
        limit: int = FOCUS_REQUESTS_PER_MINUTE,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start with empty windows; `clock` is injectable for deterministic tests."""
        self.limit = limit
        self.window = window
        self.clock = clock
        self._sent: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> bool:
        """Record one send for `key` and return True, or return False and record none.

        A send exactly `window` seconds old no longer counts.
        """
        now = self.clock()
        with self._lock:
            sent = self._sent.setdefault(key, deque())
            while sent and sent[0] <= now - self.window:
                sent.popleft()
            if len(sent) >= self.limit:
                return False
            sent.append(now)
            return True
