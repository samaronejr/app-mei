"""Two scoped passes of one authorized upload, with the quarantine PUT between."""

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
    """Only the original, already rate-limited request can own the second pass."""
    return _registration.get() is request


def dispatch_upload(
    request: HttpRequest,
    get_response: Callable[[HttpRequest], HttpResponseBase],
) -> HttpResponseBase:
    """Run the storage phase only for an upload authorized by the first pass."""
    response = get_response(request)
    if not isinstance(response, _PendingUpload):
        return response
    # The complete portal/firm scope and ATOMIC_REQUESTS have exited. Re-enter
    # the normal chain for the INSERT: membership, role, scanner and request
    # visibility are not capabilities that the preflight can carry forward.
    with response.stage.staged():
        token = _registration.set(request)
        try:
            return get_response(request)
        finally:
            _registration.reset(token)
