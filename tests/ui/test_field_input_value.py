"""The field partial writes date values the way their widget parses them (AXR1-055)."""

from datetime import date
from decimal import Decimal

import pytest
from django import forms
from django.template.loader import render_to_string
from django.utils import translation

from apps.clients.forms import DateInput
from apps.core.templatetags.field_values import input_value

PARTIAL = "partials/pure/field.html"


class _Form(forms.Form):
    starts_on = forms.DateField(widget=DateInput())
    period = forms.DateField(widget=forms.DateInput(format="%Y-%m"))
    amount = forms.DecimalField()
    name = forms.CharField()


@pytest.mark.parametrize("language", ["pt-br", "en"])
def test_a_saved_date_renders_in_its_widget_format(language: str) -> None:
    form = _Form(initial={"starts_on": date(2026, 3, 1), "period": date(2026, 9, 1)})
    with translation.override(language):
        starts = render_to_string(PARTIAL, {"field": form["starts_on"]})
        period = render_to_string(PARTIAL, {"field": form["period"]})
    assert 'type="date" value="2026-03-01"' in starts
    assert 'value="2026-09"' in period


def test_a_submitted_date_string_is_written_back_unchanged() -> None:
    form = _Form(data={"starts_on": "2026-13-40"})
    assert input_value(form["starts_on"]) == "2026-13-40"


def test_a_missing_field_renders_empty() -> None:
    assert input_value("") == ""


def test_non_date_widgets_keep_the_raw_value() -> None:
    form = _Form(initial={"amount": Decimal("1234.50"), "name": "Ana & Cia"})
    for name in ("amount", "name"):
        value = form[name].value()
        assert input_value(form[name]) == value
        assert type(input_value(form[name])) is type(value)
