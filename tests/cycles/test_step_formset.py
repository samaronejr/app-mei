"""Only genuinely untouched extra rows may bypass required-field validation."""

import pytest

from apps.cycles.forms import StepFormSet


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "partial"),
        ("label_ptbr", "Partial step"),
        ("kind", "task"),
        ("owner_rule", "team"),
        ("due_kind", "fixed_day"),
        ("due_day", "5"),
        ("evidence_required", "on"),
    ],
)
def test_partially_filled_extra_rows_still_validate(field: str, value: str) -> None:
    formset = StepFormSet(
        {
            "steps-TOTAL_FORMS": "1",
            "steps-INITIAL_FORMS": "0",
            f"steps-0-{field}": value,
        },
        prefix="steps",
    )
    extra = formset.extra_forms[0]
    assert extra.empty_permitted
    assert extra.has_changed()
    assert not formset.is_valid()
    assert any(
        error.code == "required"
        for errors in extra.errors.as_data().values()
        for error in errors
    )
