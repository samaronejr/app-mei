"""Time the complete non-streaming Django response, excluding network transport."""

import json
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter_ns

from django.conf import settings
from django.db import connection
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from ops.perf.configuration import constrained_resources, production_memory_limits


class QueryCounter:
    """Count executions without retaining SQL, parameters or client data."""

    def __init__(self) -> None:
        """Start a request-local counter at zero."""
        self.count = 0

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        """Count one execution and forward every argument unchanged."""
        self.count += 1
        return execute(sql, params, many, context)


class ServerTimer:
    """The outer middleware includes authentication, templates and commit work."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        """Retain the real middleware chain."""
        self.get_response = get_response
        self.resources = ""
        if getattr(settings, "PERF_RESOURCE_ATTESTATION", False):
            limits = production_memory_limits(Path("docker-compose.prod.yml"))
            self.resources = json.dumps(
                constrained_resources(Path("/sys/fs/cgroup"), limits["web"]),
                sort_keys=True,
            )
        self.head = Path("RELEASE").read_text().strip() if self.resources else ""

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Return elapsed time and opt-in execution counts, without retaining SQL."""
        counter = QueryCounter()
        capture = getattr(settings, "PERF_QUERY_CAPTURE", False)
        wrapper = connection.execute_wrapper(counter) if capture else nullcontext()
        start = perf_counter_ns()
        with wrapper:
            response = self.get_response(request)
        elapsed = (perf_counter_ns() - start) / 1_000_000
        response["Server-Timing"] = f"app;dur={elapsed:.6f}"
        if capture:
            response["X-Perf-Queries"] = str(counter.count)
        if self.resources:
            response["X-Perf-Resources"] = self.resources
            response["X-Perf-Head"] = self.head
        return response
