"""Scoped upload admission and registration, with quarantine I/O between."""

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps

from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from apps.cycles.intake import QuarantineRequiredError

_registration: ContextVar[HttpRequest | None] = ContextVar(
    "register_upload", default=None
)


class _PendingUpload(HttpResponse):
    def __init__(self, stage: QuarantineRequiredError) -> None:
        # Never claim success if the outer dispatcher is missing.
        super().__init__(status=503)
        self.stage = stage


def phased_upload[**P](view: Callable[P, HttpResponse]) -> Callable[P, HttpResponse]:
    """Surface the plan only after all of the view's ordinary admission checks."""

    @wraps(view)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> HttpResponse:
        try:
            return view(*args, **kwargs)
        except QuarantineRequiredError as required:
            return _PendingUpload(required)

    return wrapped


def registration_pass(request: HttpRequest) -> bool:
    """Only the original, already rate-limited request may re-enter the chain."""
    return _registration.get() is request


def dispatch_upload(
    request: HttpRequest,
    get_response: Callable[[HttpRequest], HttpResponseBase],
) -> HttpResponseBase:
    """Run the storage phase only for an upload authorized by the first pass."""
    response = get_response(request)
    if not isinstance(response, _PendingUpload):
        return response

    # ATOMIC_REQUESTS and the scoped role have unwound. Re-enter them under
    # the mutex: membership, scanner and request visibility can all change.
    def register() -> HttpResponseBase:
        result = get_response(request)
        if isinstance(result, _PendingUpload):
            raise result.stage
        return result

    token = _registration.set(request)
    try:
        return response.stage.complete(register)
    finally:
        _registration.reset(token)
