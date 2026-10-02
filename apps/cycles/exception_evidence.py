"""Resolve private exception identities into real, client-scoped staff destinations."""

from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

from django.urls import reverse

from apps.cycles.models import EvidenceLink, EvidenceRequest, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import (
    CERTIFICATE_EXPIRING,
    NO_RESPONSIBLE,
    REQUEST_OVERDUE,
    TAX_PROFILE_UNKNOWN,
)
from apps.delivery.kinds import INVITE_REJECTED
from apps.delivery.models import OutboundMessage

DOCUMENT_REASONS = frozenset({"documento_de_outra_empresa", "verificacao_indisponivel"})
DELIVERY_REASONS = frozenset({"delivery_unknown", "delivery_rejected"})


@dataclass(frozen=True, slots=True)
class EvidenceDestination:
    """A server-built route and escaped display text, never a URL from JSON."""

    url: str
    label: str


def exception_location(
    cycle: ServiceCycle, exception_id: UUID | None, origin: str
) -> str:
    """Keep the same client/period surface when opening, closing or acting."""
    params = {}
    if origin == "cliente":
        path = reverse("client-detail", args=[cycle.client_id])
        params["periodo"] = f"{cycle.period_start:%Y-%m}"
    else:
        path = reverse("cycle-detail", args=[cycle.pk])
    if exception_id is not None:
        params["pendencia"] = str(exception_id)
    query = f"?{urlencode(params)}" if params else ""
    return f"{path}{query}#pendencias"


def _references(problem: CycleException) -> set[UUID]:
    raw = next(iter(problem.internals.all())).evidence_refs
    valid: set[UUID] = set()
    if not isinstance(raw, list):
        return valid
    for value in raw:
        if not isinstance(value, str):
            continue
        try:
            valid.add(UUID(value))
        except ValueError:
            continue
    return valid


def _document_destinations(
    problems: list[CycleException], references: dict[UUID, set[UUID]]
) -> dict[UUID, list[EvidenceDestination]]:
    documents = [row for row in problems if row.reason_code in DOCUMENT_REASONS]
    if not documents:
        return {}
    intake_ids = {row.subject_ref for row in documents}
    links: dict[tuple[UUID, UUID], list[tuple[UUID, EvidenceDestination]]] = {}
    for row in (
        EvidenceLink.objects.filter(
            client_id=documents[0].client_id,
            request__cycle_id__in={problem.cycle_id for problem in documents},
            document__intakes__pk__in=intake_ids,
        )
        .order_by("created_at", "pk")
        .values_list(
            "document__intakes__pk",
            "request__cycle_id",
            "document_id",
            "pk",
            "document__original_filename",
            "request__title",
        )
    ):
        intake_id, cycle_id, document_id, link_id, filename, title = row
        links.setdefault((intake_id, cycle_id), []).append(
            (
                document_id,
                EvidenceDestination(
                    reverse("document-review", args=[link_id]),
                    f"Revisar {filename} · {title}",
                ),
            )
        )
    return {
        problem.pk: [
            destination
            for document_id, destination in links.get(
                (problem.subject_ref, problem.cycle_id), []
            )
            if document_id in references[problem.pk]
        ]
        for problem in documents
    }


def _delivery_destinations(
    problems: list[CycleException], references: dict[UUID, set[UUID]]
) -> dict[UUID, list[EvidenceDestination]]:
    deliveries = [
        row
        for row in problems
        if row.reason_code in DELIVERY_REASONS or row.reason_code == INVITE_REJECTED
    ]
    if not deliveries:
        return {}
    candidates = {
        row.pk: references[row.pk]
        if row.reason_code == INVITE_REJECTED
        else {row.subject_ref}
        for row in deliveries
    }
    identifiers: set[UUID] = set().union(*candidates.values())
    messages = {
        row[0]: row[1:]
        for row in OutboundMessage.objects.filter(
            client_id=deliveries[0].client_id, pk__in=identifiers
        ).values_list("pk", "cycle_id", "invite_id", "invite__client_id", "subject")
    }
    result: dict[UUID, list[EvidenceDestination]] = {}
    for problem in deliveries:
        result[problem.pk] = []
        for message_id in sorted(candidates[problem.pk]):
            message = messages.get(message_id)
            if message is None:
                continue
            cycle_id, invite_id, invite_client, subject = message
            matches = (
                invite_id == problem.subject_ref and invite_client == problem.client_id
                if problem.reason_code == INVITE_REJECTED
                else cycle_id == problem.cycle_id
            )
            if matches:
                result[problem.pk].append(
                    EvidenceDestination(
                        f"{reverse('delivery-queue')}?mensagem={message_id}"
                        f"#message-{message_id}",
                        f"Ver envio: {subject}",
                    )
                )
    return result


def _subject_destinations(
    problems: list[CycleException],
) -> dict[UUID, list[EvidenceDestination]]:
    subjects = [
        row
        for row in problems
        if row.reason_code
        in {REQUEST_OVERDUE, CERTIFICATE_EXPIRING, TAX_PROFILE_UNKNOWN, NO_RESPONSIBLE}
    ]
    if not subjects:
        return {}
    client_id = subjects[0].client_id
    request_ids = {
        row.subject_ref for row in subjects if row.reason_code == REQUEST_OVERDUE
    }
    requests = {
        row[0]: row[1:]
        for row in EvidenceRequest.objects.filter(
            client_id=client_id, pk__in=request_ids
        ).values_list("pk", "cycle_id", "title")
    }
    result: dict[UUID, list[EvidenceDestination]] = {}
    for problem in subjects:
        destinations = []
        cycle = problem.cycle
        workspace = (
            f"{reverse('client-detail', args=[client_id])}"
            f"?periodo={cycle.period_start:%Y-%m}"
        )
        if problem.reason_code == REQUEST_OVERDUE:
            request = requests.get(problem.subject_ref)
            if request is not None and request[0] == problem.cycle_id:
                destinations.append(
                    EvidenceDestination(
                        f"{reverse('cycle-detail', args=[problem.cycle_id])}"
                        f"#solicitacao-{problem.subject_ref}",
                        f"Ver solicitação: {request[1]}",
                    )
                )
        elif (
            problem.reason_code == CERTIFICATE_EXPIRING
            and problem.subject_ref == client_id
        ):
            destinations.append(
                EvidenceDestination(
                    f"{workspace}#certificado", "Ver validade do certificado"
                )
            )
        elif (
            problem.reason_code == TAX_PROFILE_UNKNOWN
            and problem.subject_ref == problem.cycle_id
        ):
            destinations.append(
                EvidenceDestination(
                    f"{workspace}#perfil-tributario", "Ver perfil desta competência"
                )
            )
        elif (
            problem.reason_code == NO_RESPONSIBLE
            and problem.subject_ref == cycle.engagement_id
        ):
            engagement = cycle.engagement
            if engagement.starts_on <= cycle.period_start and (
                engagement.ends_on is None or engagement.ends_on > cycle.period_start
            ):
                destinations.append(
                    EvidenceDestination(
                        f"{workspace}#servico-{engagement.pk}",
                        "Ver serviço e responsável",
                    )
                )
        result[problem.pk] = destinations
    return result


def evidence_destinations(
    problems: list[CycleException], may_review: bool
) -> dict[UUID, list[EvidenceDestination]]:
    """Batch nonempty families for one already-authorized client/cycle scope."""
    result: dict[UUID, list[EvidenceDestination]] = {row.pk: [] for row in problems}
    references = {row.pk: _references(row) for row in problems}
    if may_review:
        result.update(_document_destinations(problems, references))
    result.update(_delivery_destinations(problems, references))
    result.update(_subject_destinations(problems))
    return result
