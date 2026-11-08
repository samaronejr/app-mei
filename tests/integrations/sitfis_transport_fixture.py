"""016-owned admission for synthetic transport peers with no report or call row."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime

import pytest

from apps.integrations.call_dispatch import _wire_window
from tests.fakes import provider


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol() -> Iterator[None]:
    original = _wire_window

    def window(
        provider_name: str,
        environment: str,
        operation: str,
        lease_until: datetime,
    ) -> AbstractContextManager[object]:
        if operation == "SP-SIT-R":
            return original(
                provider_name,
                environment,
                operation,
                lease_until,
                guard=nullcontext,
            )
        return original(provider_name, environment, operation, lease_until)

    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(provider, "_wire_window", window)
        yield
