"""A form control's value, written the way its widget reads it back (AXR1-055)."""

from django import forms, template

register = template.Library()

DATE_LIKE_WIDGETS = (forms.DateInput, forms.DateTimeInput, forms.TimeInput)


@register.filter
def input_value(field: object) -> object:
    """Return a date-like widget's own formatted value and any other value unchanged.

    The field partial writes this into `value="..."`. Left to the template engine, a
    `date` initial renders in the active locale ("1 de Março de 2026"), which an
    `<input type="date">` rejects: the browser shows an empty control, a save demands
    the date again, and re-saving clears an optional date. The widget's `format_value`
    applies its declared format instead (ISO for the date picker). Every other widget
    keeps exactly the value the template rendered before.

    Anything that is not a bound field renders empty, as `{{ field.value }}` did for a
    missing `field`: the partial must still render from a context that lacks one.
    """
    if not isinstance(field, forms.BoundField):
        return ""
    value = field.value()
    widget = field.field.widget
    if isinstance(widget, DATE_LIKE_WIDGETS):
        return widget.format_value(value)
    return value
