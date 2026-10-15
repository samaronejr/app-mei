"""AXR1-037: the per-read path cache returns exactly what reverse() returns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

import pytest
from django.urls import get_script_prefix, reverse, set_script_prefix

from apps.briefing.providers import _Paths

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

UUID_ROUTES: Final = (
    "client-detail",
    "cycle-detail",
    "approval-card",
    "document-review",
)
IDENTIFIERS: Final = (
    UUID(int=0),
    UUID("12345678-1234-5678-1234-567812345678"),
    UUID(int=2**128 - 1),
)
PREFIXES: Final = (
    "/",
    "/mount/",
    f"/mount/{UUID(int=0)}/",
    f"/{UUID(int=0)}/{UUID(int=0)}/",
)


@pytest.fixture
def script_prefix() -> Iterator[Callable[[str], None]]:
    original = get_script_prefix()
    yield set_script_prefix
    set_script_prefix(original)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_cached_paths_equal_reverse_under_any_script_prefix(
    prefix: str, script_prefix: Callable[[str], None]
) -> None:
    script_prefix(prefix)
    paths = _Paths()

    for name in UUID_ROUTES:
        for identifier in IDENTIFIERS:
            assert paths(name, identifier) == reverse(name, args=[identifier])
    assert paths("delivery-queue") == reverse("delivery-queue")


def test_shapes_the_cache_cannot_prove_fall_back_to_reverse(
    script_prefix: Callable[[str], None],
) -> None:
    script_prefix(f"/mount/{UUID(int=0)}/")
    paths = _Paths()
    identifier = str(IDENTIFIERS[1])

    assert paths("client-detail", identifier) == reverse(
        "client-detail", args=[identifier]
    )
