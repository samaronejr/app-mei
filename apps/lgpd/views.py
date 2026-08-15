"""Intake for LGPD data-subject rights requests.

Unauthenticated by design: art. 18 rights belong to a data subject, not to an account
holder, and many of the people this product processes data about — the MEI clients of
its firms — have no login here at all. Requiring one would put an access wall in front
of a legal obligation.
"""

from functools import partial
from http import HTTPStatus
from uuid import UUID

import sentry_sdk
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.audit.models import AuditAction, DataSubjectRequest
from apps.audit.services import Origin, record_platform_event
from apps.core.netaddr import client_ip
from apps.lgpd.forms import DataSubjectRequestForm

FORM_TEMPLATE = "lgpd/request_form.html"
RECEIVED_TEMPLATE = "lgpd/request_received.html"


@require_http_methods(["GET", "POST"])
def data_subject_request_view(request: HttpRequest) -> HttpResponse:
    """Accept a rights request, persist it, and notify the encarregado."""
    if request.method == "GET":
        return render(request, FORM_TEMPLATE, {"form": DataSubjectRequestForm()})

    form = DataSubjectRequestForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            FORM_TEMPLATE,
            {"form": form},
            status=HTTPStatus.BAD_REQUEST,
        )

    dsr = form.save()
    transaction.on_commit(partial(_notify_encarregado_safe, dsr.pk))
    # A PlatformEvent, not an Event: the requester is anonymous and no tenant has been
    # resolved, so the tenant-scoped table would reject this row outright. The event
    # carries the request's id — never the CPF, which lives in an erasable table.
    record_platform_event(
        action=AuditAction.DSR_SUBMITTED,
        origin=Origin(subject=dsr.email, ip=client_ip(request)),
        metadata={"request_id": str(dsr.pk), "request_type": dsr.request_type},
    )
    return HttpResponseRedirect(reverse("dsr-received"))


def _notify_encarregado_safe(request_id: UUID) -> None:
    try:
        # PLATFORM_QUERY_OK: this post-commit callback receives the UUID of the legal
        # request the same view just saved; it neither lists nor accepts a tenant row.
        dsr = DataSubjectRequest.objects.get(pk=request_id)
        _notify_encarregado(dsr)
        # PLATFORM_QUERY_OK: stamp only that callback-owned platform request UUID.
        DataSubjectRequest.objects.filter(pk=request_id).update(
            encarregado_notified_at=now(),
        )
    except Exception as error:  # noqa: BLE001
        try:
            # PLATFORM_QUERY_OK: persist delivery status only on the same callback UUID.
            DataSubjectRequest.objects.filter(pk=request_id).update(
                notification_last_error=str(error),
            )
        except Exception as persistence_error:  # noqa: BLE001
            _capture_exception_safely(persistence_error)
        _capture_exception_safely(error)


def _capture_exception_safely(error: Exception) -> None:
    try:
        sentry_sdk.capture_exception(error)
    except Exception:  # noqa: BLE001
        return


def _notify_encarregado(dsr: DataSubjectRequest) -> None:
    send_mail(
        subject=_("Nova solicitação LGPD: %(kind)s") % {"kind": dsr.request_type},
        message=_(
            "Uma solicitação de titular foi registrada.\n\n"
            "Identificador: %(id)s\nTipo: %(kind)s\nRelação: %(rel)s\n",
        )
        % {"id": dsr.pk, "kind": dsr.request_type, "rel": dsr.relationship},
        from_email=None,
        recipient_list=[settings.LGPD_ENCARREGADO_EMAIL],
    )


def request_received_view(request: HttpRequest) -> HttpResponse:
    """Confirm receipt and state the statutory response deadline."""
    return render(request, RECEIVED_TEMPLATE, {})
