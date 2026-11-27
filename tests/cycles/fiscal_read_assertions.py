"""Measure the fiscal statements used to build one preview."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.records import ServiceCycle
from apps.obligations.models import FiscalDocument, FiscalRevision, RevisionArtifact

if TYPE_CHECKING:
    from collections.abc import Iterator


class FiscalReads:
    """Measure statements supplying preview facts, not a dispatch or locked write."""

    def __init__(self) -> None:
        self.captures: list[CaptureQueriesContext] = []
        self.writer_queries: set[int] = set()

    @contextmanager
    def capture(self) -> Iterator[None]:
        with CaptureQueriesContext(connection) as queries:
            self.captures.append(queries)
            yield

    @contextmanager
    def writer(self) -> Iterator[None]:
        (queries,) = self.captures
        start = len(queries)
        yield
        self.writer_queries.update(range(start, len(queries)))

    def assert_snapshot(self) -> None:
        (queries,) = self.captures
        tables = {
            f'"{model._meta.db_table}"'
            for model in (
                FiscalDocument,
                FiscalRevision,
                RevisionArtifact,
                ServiceCycle,
                FiscalDocumentReview,
            )
        }
        reads = [
            row["sql"]
            for i, row in enumerate(queries)
            if i not in self.writer_queries
            and row["sql"].lstrip().upper().startswith("SELECT")
            and any(table in row["sql"] for table in tables)
        ]
        assert len(reads) == 1
        assert not {table for table in tables if table not in reads[0]}
