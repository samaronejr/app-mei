"""The guide's provider-recovery registration never admits bank import."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.scope import test_scope_fidelity as scope_fidelity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("injection", "refusal"),
    [
        ("", None),
        ("ofx", "bank-import reference"),
        ("foreign", "reconciliation outside provider recovery"),
    ],
    ids=["registered-provider-state", "ofx-in-provider-file", "unregistered-file"],
)
def test_provider_recovery_registration_keeps_bank_import_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injection: str,
    refusal: str | None,
) -> None:
    # Exercise the real guard, including its vacuity and exact-file assertions.
    for index in range(101):
        decoy = tmp_path / "apps" / f"sample_{index}.py"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text("", encoding="utf-8")
    for filename in scope_fidelity.PROVIDER_RECOVERY_FILES:
        source = tmp_path / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# reconcil\n", encoding="utf-8")
    if injection == "ofx":
        source = tmp_path / "apps/integrations/serpro/pgmei.py"
        source.write_text("# reconcil\n# OFX\n", encoding="utf-8")
    elif injection == "foreign":
        source = tmp_path / "apps/unregistered.py"
        source.write_text("# reconcil\n", encoding="utf-8")
    monkeypatch.setattr(scope_fidelity, "PROJECT_ROOT", tmp_path)
    if refusal is None:
        scope_fidelity.test_row_6_no_bank_import()
    else:
        with pytest.raises(AssertionError, match=refusal):
            scope_fidelity.test_row_6_no_bank_import()
