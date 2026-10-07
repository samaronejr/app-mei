"""Document queue and review workspace (T13, S5.4 M5, S7.5).

`/documentos/` lists submitted files in three queues (`QueueSpec`, the obligations
queue pattern): waiting for review, divergent (another company's CNPJ) and still in
verification (scan or extraction pending). `/documentos/<link>/revisar/` shows one
submission: the preview, the fields table (suggested / reviewed / origin), and the
accept / reject decision whose pt-BR reason the client reads in the portal.

Gating: `require_can(cycles.review)` (S8.1, FULL/NONE), then every row and every id
goes through the actor's portfolio (`visible_clients` / `load_visible_client`): an
unassigned client's file is 404, never a 403 that confirms it exists. The decision is
`record_review`, which repeats the scope and the capability inside its locks.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Final
from uuid import UUID

import sentry_sdk
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.accounts.views import AuthenticatedRequest
from apps.authz.objects import load_visible_client
from apps.authz.portfolio import visible_clients
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.evidence import (
    CLEAN_STATES,
    PENDING_SCAN_STATES,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExtractedField,
    FieldOrigin,
    IntakeState,
    RequestState,
    ReviewDecision,
)
from apps.cycles.extraction import FIELD_LABELS, KIND_FIELDS, XML_LAYOUTS, kind_for
from apps.cycles.receipt_observations import payment_evidence_eligible
from apps.cycles.services.cycle import require_enabled, tenant_id
from apps.cycles.services.review import FieldCorrection, record_review
from apps.cycles.serving import document_response
from apps.cycles.sniffing import JPEG, PNG
from apps.obligations.views import QueueSpec

PAGE_SIZE: Final = 25
QUEUE_PARAM: Final = "fila"
FIELD_PREFIX: Final = "campo_"
IMAGE_TYPES: Final = frozenset({PNG, JPEG})
OPEN_REQUEST_STATES: Final = frozenset(RequestState.values) - {
    RequestState.WAIVED,
    RequestState.REPLACED,
}
# Waiting for a person: extraction finished, or can never run (unscanned file).
AWAITING_STATES: Final = frozenset(
    {IntakeState.CLASSIFIED, IntakeState.LINKED, IntakeState.SCAN_UNAVAILABLE}
)
IN_VERIFICATION: Final = PENDING_SCAN_STATES | {IntakeState.VERIFIED}

# pt-BR for the machine codes the extraction stores in `DocumentIntake.reason`.
# Nothing here says "verificado": a suggestion is a suggestion (S5.7, invariant 8).
NOTES: Final[dict[str, str]] = {
    "layout_nao_reconhecido": (
        "Layout não reconhecido: nenhum campo foi lido. Confira o documento e "
        "preencha a revisão."
    ),
    "nao_verificavel": (
        "Não verificável automaticamente: nenhum CNPJ foi lido deste arquivo. "
        "Confira a empresa antes de aceitar."
    ),
    "pdf_sem_texto": (
        "PDF sem texto legível (provavelmente digitalizado). Não verificável "
        "automaticamente."
    ),
    "extracao_interrompida": (
        "A leitura automática passou do tempo limite e foi interrompida. Não "
        "verificável automaticamente."
    ),
    "cnpj_divergente": (
        "O CNPJ lido no documento não é o deste cliente. O arquivo está suspenso "
        "e uma pendência foi aberta. Ele não foi enviado a nenhum outro cliente."
    ),
}
UNREADABLE_NOTE: Final = (
    "O arquivo não pôde ser lido com segurança pela leitura automática."
)
UNSCANNED_NOTE: Final = (
    "Arquivo sem verificação antivírus: nenhum campo foi lido. Baixe somente com o "
    "aviso de risco."
)
ORIGIN_LABELS: Final[dict[str, str]] = {
    FieldOrigin.XML: "Sugerido: XML da nota",
    FieldOrigin.PDF_TEXT: "Sugerido: texto do PDF",
    FieldOrigin.STAFF: "Revisado pela equipe",
    FieldOrigin.CLIENT: "Informado pelo cliente",
}


def _links(actor: User) -> QuerySet[EvidenceLink]:
    """Every submission in the actor's book, with its intake state on the row."""
    clients = visible_clients(actor, tenant_id=tenant_id())
    return (
        EvidenceLink.objects.filter(
            client_id__in=clients.values("pk"),
            request__state__in=OPEN_REQUEST_STATES,
        )
        .select_related("document", "request", "client")
        .annotate(
            intake_state=Subquery(
                DocumentIntake.objects.filter(document=OuterRef("document_id")).values(
                    "state"
                )[:1]
            )
        )
        .order_by("created_at", "pk")
    )


def awaiting_review(actor: User) -> QuerySet[EvidenceLink]:
    """Files a reviewer can decide on now (a vault upload has no intake at all)."""
    return _links(actor).filter(
        Q(intake_state__in=AWAITING_STATES) | Q(intake_state__isnull=True),
        review_decision=ReviewDecision.NONE,
    )


def divergent(actor: User) -> QuerySet[EvidenceLink]:
    """Files whose CNPJ is another company's, not yet decided."""
    return _links(actor).filter(
        Q(intake_state=IntakeState.DIVERGENT), review_decision=ReviewDecision.NONE
    )


def in_verification(actor: User) -> QuerySet[EvidenceLink]:
    """Files the scanner or the extraction has not finished with."""
    return _links(actor).filter(Q(intake_state__in=IN_VERIFICATION))


HEADINGS: Final[tuple[object, ...]] = (
    _("Cliente"),
    _("Solicitação"),
    _("Arquivo"),
    _("Enviado em"),
    _("Situação"),
)
PENDING = QueueSpec(
    "pendentes",
    _("Aguardando revisão"),
    CYCLES_REVIEW,
    awaiting_review,
    "cycles/_document_rows.html",
    HEADINGS,
)
DIVERGENT = QueueSpec(
    "divergentes",
    _("Divergentes"),
    CYCLES_REVIEW,
    divergent,
    "cycles/_document_rows.html",
    HEADINGS,
)
VERIFYING = QueueSpec(
    "verificacao",
    _("Em verificação"),
    CYCLES_REVIEW,
    in_verification,
    "cycles/_document_rows.html",
    HEADINGS,
)
DOCUMENT_QUEUES: Final = (PENDING, DIVERGENT, VERIFYING)


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can(CYCLES_REVIEW)
def document_queue(request: AuthenticatedRequest) -> HttpResponse:
    """Render the three document queues, one at a time, each with its count."""
    require_enabled()
    selected = next(
        (spec for spec in DOCUMENT_QUEUES if spec.slug == request.GET.get(QUEUE_PARAM)),
        PENDING,
    )
    tabs = [
        {"spec": spec, "count": spec.fetch(request.user).count()}
        for spec in DOCUMENT_QUEUES
    ]
    page = Paginator(selected.fetch(request.user), PAGE_SIZE).get_page(
        request.GET.get("pagina")
    )
    return render(
        request,
        "cycles/document_queue.html",
        {
            "spec": selected,
            "tabs": tabs,
            "page": page,
            "columns": [{"label": heading} for heading in selected.headings],
            "queue_param": QUEUE_PARAM,
        },
    )


@dataclass(frozen=True, slots=True)
class FieldRow:
    """One row of the fields table: suggestion, reviewed value and provenance."""

    name: str
    label: str
    suggested: str
    suggested_origin: str
    source_span: str
    reviewed: str
    reviewed_by: str


def _field_rows(link: EvidenceLink, kind: str) -> list[FieldRow]:
    suggested: dict[str, ExtractedField] = {}
    reviewed: dict[str, ExtractedField] = {}
    for field in ExtractedField.objects.filter(document_id=link.document_id).order_by(
        "created_at", "pk"
    ):
        target = reviewed if field.origin == FieldOrigin.STAFF else suggested
        target[field.name] = field
    if kind == "xml":
        # An XML's layout is known from what the extractor read, not from its type.
        kind = next(
            (
                layout
                for layout in XML_LAYOUTS
                if suggested.keys() & set(KIND_FIELDS[layout][:2])
            ),
            kind,
        )
    names = [
        *KIND_FIELDS.get(kind, ()),
        *(name for name in suggested if name not in KIND_FIELDS.get(kind, ())),
    ]
    rows = []
    for name in names:
        if name not in FIELD_LABELS:
            continue
        hint = suggested.get(name)
        done = reviewed.get(name)
        rows.append(
            FieldRow(
                name=name,
                label=FIELD_LABELS[name],
                suggested=hint.value if hint else "",
                suggested_origin=ORIGIN_LABELS.get(hint.origin, "") if hint else "",
                source_span=hint.source_span if hint else "",
                reviewed=done.value if done else "",
                reviewed_by=ORIGIN_LABELS[FieldOrigin.STAFF] if done else "",
            )
        )
    return rows


def _note(intake: DocumentIntake | None) -> str:
    if intake is None or intake.state == IntakeState.SCAN_UNAVAILABLE:
        return UNSCANNED_NOTE
    if intake.reason.startswith("arquivo_ilegivel"):
        return UNREADABLE_NOTE
    return NOTES.get(intake.reason, "")


def _visible_link(actor: User, pk: UUID) -> EvidenceLink:
    link = (
        EvidenceLink.objects.select_related("document", "request", "client")
        .filter(pk=pk)
        .first()
    )
    if link is None:
        raise Http404
    load_visible_client(actor, link.client_id)
    return link


def next_link(actor: User, current: EvidenceLink) -> EvidenceLink | None:
    """Return the next undecided file after this one, else the first before it."""
    queue = (awaiting_review(actor) | divergent(actor)).exclude(pk=current.pk)
    after = queue.filter(
        Q(created_at__gt=current.created_at)
        | Q(created_at=current.created_at, pk__gt=current.pk)
    ).first()
    return after or queue.first()


def _corrections(
    request: AuthenticatedRequest, rows: list[FieldRow]
) -> list[FieldCorrection]:
    corrections = []
    for row in rows:
        value = request.POST.get(f"{FIELD_PREFIX}{row.name}", "")
        if value.strip():
            corrections.append(
                FieldCorrection(row.name, row.suggested, value, row.suggested_origin)
            )
    return corrections


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can(CYCLES_REVIEW)
def document_review(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Review one submission; the POST carries the request revision it was drawn at."""
    require_enabled()
    link = _visible_link(request.user, pk)
    intake = DocumentIntake.objects.filter(document_id=link.document_id).first()
    kind = kind_for(intake.sniffed_type if intake else link.document.content_type)
    rows = _field_rows(link, kind)
    error, status = "", HTTPStatus.OK
    if request.method == "POST":
        try:
            expected = int(request.POST.get("expected_version", ""))
        except ValueError:
            expected = 0
        try:
            record_review(
                request.user,
                link.pk,
                decision=request.POST.get("decision", ""),
                client_reason=request.POST.get("client_reason", ""),
                corrections=_corrections(request, rows),
                expected_version=expected,
                third_party=request.POST.get("third_party") == "1",
                override_reason=request.POST.get("override_reason", ""),
                # T17: only a bound receipt consumes the flag; for every other
                # request it is inert, and the service re-checks the binding.
                payment_relevant=request.POST.get("payment_relevant") == "1",
            )
        except VersionConflict as conflict:
            error, status = str(conflict), HTTPStatus.CONFLICT
        except IllegalTransition as refused:
            error, status = str(refused), HTTPStatus.CONFLICT
        except ValidationError as invalid:
            error, status = " ".join(invalid.messages), HTTPStatus.UNPROCESSABLE_ENTITY
        else:
            following = next_link(request.user, link)
            if following is not None:
                return redirect("document-review", following.pk)
            return redirect("document-queue")
        link = _visible_link(request.user, pk)
        rows = _field_rows(link, kind)
    evidence_request = EvidenceRequest.objects.get(pk=link.request_id)
    following = next_link(request.user, link)
    clean = intake is not None and intake.state in CLEAN_STATES
    return render(
        request,
        "cycles/document_review.html",
        {
            "link": link,
            "evidence_request": evidence_request,
            "document": link.document,
            "intake": intake,
            "rows": rows,
            "note": _note(intake),
            "divergent": intake is not None and intake.state == IntakeState.DIVERGENT,
            "clean": clean,
            "payment_eligible": payment_evidence_eligible(
                evidence_request, link, intake
            ),
            "inline_image": clean and link.document.content_type in IMAGE_TYPES,
            "decided": bool(link.review_decision),
            "next_link": following,
            "error": error,
            "field_prefix": FIELD_PREFIX,
            "queue_url": reverse("document-queue"),
        },
        status=status,
    )


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can(CYCLES_REVIEW)
def document_preview(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Serve a clean document inline, sandboxed (T12 headers); anything else is 404."""
    require_enabled()
    link = _visible_link(request.user, pk)
    intake = DocumentIntake.objects.filter(document_id=link.document_id).first()
    if intake is None or intake.state not in CLEAN_STATES:
        raise Http404
    document = link.document
    try:
        with default_storage.open(document.storage_key, "rb") as handle:
            payload = handle.read()
    except OSError as error:
        sentry_sdk.capture_exception(error)
        return HttpResponse(
            "Documento indisponível no momento.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return document_response(
        payload,
        content_type=document.content_type,
        filename=document.original_filename,
        inline=True,
    )
