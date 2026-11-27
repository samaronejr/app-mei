"""The only input binding for the fiscal review workspace."""

from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest

CYCLE_QUERY: Final = "ciclo"
CYCLE_FIELD: Final = "cycle_id"
INVALID_SCOPE: Final = "Selecione um ciclo e confira a revisão antes de salvar."


def submitted_cycle(request: HttpRequest) -> UUID | None:
    """Only a bare GET can choose a default; URL/body must otherwise be explicit."""
    query = request.GET.getlist(CYCLE_QUERY)
    if len(query) > 1 or CYCLE_FIELD in request.GET:
        raise ValidationError(INVALID_SCOPE)
    requested = _parse_cycle(query[0]) if query else None
    if request.method == "GET":
        # GET has no form-body protocol on this route. Do not silently discard
        # even a duplicate selector carried in an otherwise valid request.
        if request.body:
            raise ValidationError(INVALID_SCOPE)
        return requested
    body = request.POST.getlist(CYCLE_FIELD)
    if len(body) != 1 or CYCLE_QUERY in request.POST:
        raise ValidationError(INVALID_SCOPE)
    submitted = _parse_cycle(body[0])
    if requested is not None and submitted != requested:
        raise Http404
    return submitted


def _parse_cycle(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as bad_cycle:
        raise ValidationError(INVALID_SCOPE) from bad_cycle
