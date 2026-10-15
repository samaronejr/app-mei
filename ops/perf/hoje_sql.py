"""Capture executed SQL and boundary inputs without replacing application queries."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from time import perf_counter_ns
from typing import TYPE_CHECKING, Final
from uuid import UUID

import psycopg
from django.db import connection

if TYPE_CHECKING:
    from django.test.utils import CaptureQueriesContext

type Parameters = Sequence[object] | Mapping[str, object] | None

BOUNDARY_SQL: Final = """
SELECT jsonb_build_object(
    'captured_at', clock_timestamp(),
    'database', current_database(),
    'role', current_user,
    'tenant_id', coalesce(current_setting('app.tenant_id', true), ''),
    'client_id', coalesce(current_setting('app.client_id', true), ''),
    'search_path', current_setting('search_path'),
    'row_security', current_setting('row_security'),
    'timezone', current_setting('TimeZone'),
    'planner_settings', (
        SELECT jsonb_object_agg(name, setting)
        FROM pg_settings
        WHERE name IN (
            'work_mem', 'effective_cache_size', 'random_page_cost',
            'seq_page_cost', 'default_statistics_target', 'jit',
            'jit_above_cost', 'enable_nestloop', 'enable_hashjoin',
            'enable_mergejoin', 'plan_cache_mode',
            'max_parallel_workers_per_gather', 'track_io_timing'
        )
    ),
    'tables', (
        SELECT coalesce(jsonb_agg(jsonb_build_object(
            'schema', n.nspname, 'table', c.relname, 'oid', c.oid,
            'reltuples', c.reltuples, 'relpages', c.relpages,
            'n_live_tup', s.n_live_tup, 'n_dead_tup', s.n_dead_tup,
            'n_mod_since_analyze', s.n_mod_since_analyze,
            'last_analyze', s.last_analyze,
            'last_autoanalyze', s.last_autoanalyze,
            'relrowsecurity', c.relrowsecurity,
            'relforcerowsecurity', c.relforcerowsecurity,
            'reloptions', c.reloptions
        ) ORDER BY c.relname), '[]'::jsonb)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    ),
    'indexes', (
        SELECT coalesce(jsonb_agg(jsonb_build_object(
            'table', tablename, 'name', indexname, 'definition', indexdef
        ) ORDER BY tablename, indexname), '[]'::jsonb)
        FROM pg_indexes WHERE schemaname = 'public'
    )
)::text
"""


def driver_connection() -> psycopg.Connection[tuple[object, ...]]:
    """Return the request's actual psycopg connection, not an elevated session."""
    connection.ensure_connection()
    driver = connection.connection
    if not isinstance(driver, psycopg.Connection):
        message = "Hoje investigation requires the PostgreSQL psycopg backend."
        raise TypeError(message)
    return driver


def boundary_inputs() -> dict[str, object]:
    """Read all public relation statistics immediately before the observed query."""
    # Bypass Django's debug cursor and execute wrappers ONLY for instrumentation.
    # This keeps diagnostic SQL out of application query counts and avoids recursion.
    with driver_connection().cursor() as cursor:
        cursor.execute(BOUNDARY_SQL, prepare=False)
        row = cursor.fetchone()
    if row is None:
        message = "Missing planner boundary inputs."
        raise RuntimeError(message)
    return dict(json.loads(str(row[0])))


def encode_parameter(value: object) -> dict[str, str]:
    """Preserve non-JSON parameter types without truncation or repr fallbacks."""
    if isinstance(value, (datetime, date)):
        text = value.isoformat()
    elif isinstance(value, (UUID, Decimal, IPv4Address, IPv6Address)):
        text = str(value)
    elif isinstance(value, (bytes, memoryview)):
        text = base64.b64encode(bytes(value)).decode("ascii")
    else:
        message = f"Unsupported evidence parameter type: {type(value).__qualname__}"
        raise TypeError(message)
    return {"python_type": type(value).__qualname__, "value": text}


@dataclass(frozen=True, slots=True)
class Statement:
    """Retain original parameters for same-connection, read-only plan replay."""

    log_index: int
    sql: str
    params: Parameters
    many: bool
    wall_ms: float
    started_at: str
    inputs: dict[str, object]

    def evidence(self, captured: CaptureQueriesContext) -> dict[str, object]:
        """Pair the exact parameterized call with Django's complete rendered SQL."""
        return {
            "capture_index": self.log_index,
            "sql": self.sql,
            "parameters": {
                "python_type": type(self.params).__qualname__,
                "value": self.params,
            },
            "many": self.many,
            "executed_sql": captured.captured_queries[self.log_index]["sql"],
            "execute_wall_ms": self.wall_ms,
            "started_at": self.started_at,
            "planner_inputs_before": self.inputs,
        }


@dataclass(slots=True)
class SQLCapture:
    """Observe calls once, with catalog collection outside statement wall timing."""

    log_start: int
    statements: list[Statement] = field(default_factory=list)

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: Parameters,
        many: bool,
        context: dict[str, object],
    ) -> object:
        """Forward the application call unchanged and retain its elapsed wall time."""
        inputs = boundary_inputs()
        index = len(connection.queries_log) - self.log_start
        started_at = datetime.now(UTC).isoformat()
        start = perf_counter_ns()
        try:
            return execute(sql, params, many, context)
        finally:
            elapsed = (perf_counter_ns() - start) / 1_000_000
            self.statements.append(
                Statement(index, sql, params, many, elapsed, started_at, inputs)
            )


def explain_statement(statement: Statement) -> dict[str, object]:
    """Replay a captured SELECT under its RLS context in a read-only transaction."""
    if statement.inputs["role"] != "app_runtime":
        message = "Only app_runtime SELECTs may be replayed."
        raise RuntimeError(message)
    driver = driver_connection()
    with driver.transaction(), driver.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY", prepare=False)
        for guc, key in (
            ("app.tenant_id", "tenant_id"),
            ("app.client_id", "client_id"),
            ("search_path", "search_path"),
            ("row_security", "row_security"),
            ("TimeZone", "timezone"),
        ):
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                (guc, str(statement.inputs[key])),
                prepare=False,
            )
        inputs = boundary_inputs()
        start = perf_counter_ns()
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement.sql,
            statement.params,
            prepare=False,
        )
        plan = cursor.fetchone()
        elapsed = (perf_counter_ns() - start) / 1_000_000
    if plan is None:
        message = "EXPLAIN returned no JSON plan."
        raise RuntimeError(message)
    return {
        "capture_index": statement.log_index,
        "sql": statement.sql,
        "parameters": {
            "python_type": type(statement.params).__qualname__,
            "value": statement.params,
        },
        "original_execute_wall_ms": statement.wall_ms,
        "explain_wall_ms": elapsed,
        "planner_inputs_before": inputs,
        "plan": plan[0],
        "same_seed": True,
        "same_cache_state": False,
    }


def slowest_selects(statements: list[Statement], limit: int) -> list[Statement]:
    """Rank read statements, excluding GUC operations and all write/control SQL."""
    reads = [
        statement
        for statement in statements
        if statement.sql.lstrip().upper().startswith("SELECT ")
        and "set_config(" not in statement.sql.lower()
        and not statement.many
    ]
    return sorted(reads, key=lambda statement: statement.wall_ms, reverse=True)[:limit]
