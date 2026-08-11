"""Read and constrain the immutable release identity baked into the image."""

import re
from enum import StrEnum, auto
from functools import cache
from pathlib import Path
from typing import Final

RELEASE_PATH: Final = Path("/app/RELEASE")
_RELEASE_PATTERN: Final = re.compile(r"[0-9a-f]{40}")


class _ReleaseFallback(StrEnum):
    UNKNOWN = auto()


UNKNOWN_RELEASE: Final = _ReleaseFallback.UNKNOWN.value


@cache
def current_release() -> str:
    """Return the safe release identity captured when this module was imported."""
    try:
        release = RELEASE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN_RELEASE
    if _RELEASE_PATTERN.fullmatch(release) is None:
        return UNKNOWN_RELEASE
    return release


_IMPORTED_RELEASE: Final = current_release()
