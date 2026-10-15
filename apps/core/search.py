"""Permission-scoped search for the header field and the command palette (S7.2).

One service, four kinds, no index of its own (ADR 0005). Every queryset starts from
`visible_clients(actor)` -- the same seam the queues and counts use -- so search can
show nothing the actor's screens would not, and a client outside the book is not
found by name, by CNPJ, nor through anything that hangs off it.

Matching runs in PostgreSQL through `public.search_fold` (`unaccent` + `lower`,
created by `core.0001_search_trigram_indexes`), applied to the column AND to the
query, so "sao" finds "São". The fold is IMMUTABLE so the trigram GIN indexes can be
built over it. A query shorter than two characters answers nothing; each kind
answers at most five hits and never a total, because a count of what matched is a
fact about rows the hits do not show.

Documents are searched by their file name only, never by content or by a value read
out of them (S7.5), and a document whose intake is suspended -- refused, or
`divergente`, i.e. carrying another company's CNPJ -- is never a hit at all.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Final, cast
from uuid import UUID

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import (
    BooleanField,
    Exists,
    Expression,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
)
from django.db.models.expressions import Combinable
from django.db.models.sql.compiler import SQLCompiler
from django.urls import reverse

from apps.authz.portfolio import visible_clients
from apps.authz.services import Actor, can
from apps.clients.models import ClientCompany
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.evidence import (
    SUSPENDED_STATES,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
)
from apps.cycles.records import ServiceCycle
from apps.fiscal.validators import CNPJ_ALPHABET, normalize_document
from apps.obligations.models import Document

MIN_QUERY_LENGTH: Final = 2
# Long enough for any name or CNPJ a person types; a pasted page is cut, not matched.
MAX_QUERY_LENGTH: Final = 100
PER_KIND_LIMIT: Final = 5
# Below this length a trigram similarity is mostly noise; substring match only.
FUZZY_MIN_LENGTH: Final = 4


class SearchKind(StrEnum):
    """What a hit is, in the order the palette groups them."""

    CLIENT = "client"
    CYCLE = "cycle"
    REQUEST = "request"
    DOCUMENT = "document"


KIND_LABELS: Final[dict[SearchKind, str]] = {
    SearchKind.CLIENT: "Clientes",
    SearchKind.CYCLE: "Ciclos",
    SearchKind.REQUEST: "Solicitações",
    SearchKind.DOCUMENT: "Documentos",
}
ALL_KINDS: Final = tuple(SearchKind)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result: what it is, what to call it, and where it leads."""

    kind: SearchKind
    id: UUID
    label: str
    detail: str
    url: str


class FoldedMatch(Expression):
    """`search_fold(column)` contains `search_fold(term)`, or closely matches it.

    The term is bound as a parameter, then folded and LIKE-escaped in PostgreSQL:
    `unaccent` can turn full-width characters into `%`, `_` or a backslash.
    The column side is the exact expression the trigram GIN indexes are built on,
    so the planner can use them.
    """

    conditional = True
    output_field = BooleanField()

    def __init__(self, column: str, term: str) -> None:
        """Match `term` against the folded `column`."""
        super().__init__()
        self.column: Combinable | Expression = F(column)
        self.term = term

    def get_source_expressions(self) -> list[Any]:
        """Expose the column so Django resolves its joins and table alias."""
        return [self.column]

    def set_source_expressions(self, exprs: Sequence[Combinable | Expression]) -> None:
        """Take back the resolved column."""
        (self.column,) = exprs

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, list[Any]]:
        """Substring match on the folded text, plus word similarity when long enough.

        PostgreSQL only, as is every other part of this product: `connection` names
        no vendor branch to take.
        """
        del connection
        # Resolved by now: Django swapped the F() for the table's Col.
        column, params = compiler.compile(cast("Expression", self.column))
        folded = f"public.search_fold({column})"
        escaped = (
            "replace(replace(replace(public.search_fold(%s), %s, %s), %s, %s), %s, %s)"
        )
        sql = f"{folded} LIKE '%%' || {escaped} || '%%'"
        bound = [*params, self.term, "\\", "\\\\", "%", "\\%", "_", "\\_"]
        if len(self.term) >= FUZZY_MIN_LENGTH:
            sql = f"({sql} OR public.search_fold(%s) <%% {folded})"
            bound.extend([self.term, *params])
        return sql, bound


def _term(q: str) -> str | None:
    # A query string is untrusted input: a NUL byte cannot be bound to a PostgreSQL
    # text parameter at all (the request would 500), and no control character is
    # something a person searches for, so each becomes a space before matching.
    printable = "".join(char if char.isprintable() else " " for char in q)
    term = " ".join(printable.split())[:MAX_QUERY_LENGTH]
    return term if len(term) >= MIN_QUERY_LENGTH else None


def _document_term(term: str) -> str | None:
    normalized = normalize_document(term)
    if len(normalized) < MIN_QUERY_LENGTH:
        return None
    if not all(char in CNPJ_ALPHABET for char in normalized):
        return None
    return normalized


def _matching_clients(
    clients: QuerySet[ClientCompany], term: str
) -> QuerySet[ClientCompany]:
    matches = Q(FoldedMatch("legal_name", term)) | Q(FoldedMatch("trade_name", term))
    document = _document_term(term)
    if document is not None:
        matches |= Q(cnpj__contains=document)
    return clients.filter(matches)


def _period(cycle_start: date) -> str:
    return cycle_start.strftime("%m/%Y")


def _client_hits(clients: QuerySet[ClientCompany], term: str) -> list[SearchHit]:
    rows = _matching_clients(clients, term).order_by("legal_name", "pk")[
        :PER_KIND_LIMIT
    ]
    return [
        SearchHit(
            SearchKind.CLIENT,
            row.pk,
            row.legal_name,
            row.masked_cnpj,
            reverse("client-detail", args=[row.pk]),
        )
        for row in rows
    ]


def _cycle_hits(clients: QuerySet[ClientCompany], term: str) -> list[SearchHit]:
    matched = _matching_clients(clients, term).values("pk")
    rows = (
        ServiceCycle.objects.filter(client_id__in=matched)
        .select_related("client")
        .order_by("-period_start", "client__legal_name", "pk")[:PER_KIND_LIMIT]
    )
    return [
        SearchHit(
            SearchKind.CYCLE,
            row.pk,
            f"Ciclo {_period(row.period_start)}",
            row.client.legal_name,
            reverse("cycle-detail", args=[row.pk]),
        )
        for row in rows
    ]


def _request_hits(clients: QuerySet[ClientCompany], term: str) -> list[SearchHit]:
    rows = (
        EvidenceRequest.objects.filter(client_id__in=clients.values("pk"))
        .filter(FoldedMatch("title", term))
        .select_related("client", "cycle")
        .order_by("-cycle__period_start", "title", "pk")[:PER_KIND_LIMIT]
    )
    return [
        SearchHit(
            SearchKind.REQUEST,
            row.pk,
            row.title,
            f"{row.client.legal_name} · {_period(row.cycle.period_start)}",
            reverse("cycle-detail", args=[row.cycle_id]),
        )
        for row in rows
    ]


def _document_hits(clients: QuerySet[ClientCompany], term: str) -> list[SearchHit]:
    suspended = DocumentIntake.objects.filter(
        document_id=OuterRef("pk"), state__in=SUSPENDED_STATES
    )
    newest_link = EvidenceLink.objects.filter(document_id=OuterRef("pk")).order_by(
        "-created_at", "-pk"
    )
    rows = (
        Document.objects.filter(client_id__in=clients.values("pk"))
        .filter(FoldedMatch("original_filename", term))
        .exclude(Exists(suspended))
        .annotate(link_id=Subquery(newest_link.values("pk")[:1]))
        .select_related("client")
        .order_by("-created_at", "pk")[:PER_KIND_LIMIT]
    )
    return [
        SearchHit(
            SearchKind.DOCUMENT,
            row.pk,
            row.original_filename,
            row.client.legal_name,
            reverse("document-review", args=[row.link_id])
            if row.link_id is not None
            else reverse("staff-document-download", args=[row.pk]),
        )
        for row in rows
    ]


def search(
    actor: Actor,
    q: str,
    kinds: Iterable[SearchKind] = ALL_KINDS,
    *,
    tenant_id: UUID | None = None,
) -> list[SearchHit]:
    """Return at most five hits per kind from the actor's own book, grouped by kind.

    A query under two characters answers nothing rather than everything. Documents
    lead to their linked review or the existing firm-side download gate.
    """
    term = _term(q)
    if term is None:
        return []
    clients = visible_clients(actor, tenant_id=tenant_id)
    wanted = set(kinds)
    hits: list[SearchHit] = []
    if SearchKind.CLIENT in wanted:
        hits.extend(_client_hits(clients, term))
    if SearchKind.CYCLE in wanted:
        hits.extend(_cycle_hits(clients, term))
    if SearchKind.REQUEST in wanted:
        hits.extend(_request_hits(clients, term))
    if SearchKind.DOCUMENT in wanted and can(actor, CYCLES_REVIEW, tenant_id=tenant_id):
        hits.extend(_document_hits(clients, term))
    return hits


__all__ = [
    "ALL_KINDS",
    "KIND_LABELS",
    "MIN_QUERY_LENGTH",
    "PER_KIND_LIMIT",
    "SearchHit",
    "SearchKind",
    "search",
]
