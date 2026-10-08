"""Time the complete non-streaming Django response, excluding network transport."""

import json
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from ops.perf.configuration import constrained_resources, production_memory_limits


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

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Return server elapsed time without collecting SQL or changing behavior."""
        start = perf_counter_ns()
        response = self.get_response(request)
        elapsed = (perf_counter_ns() - start) / 1_000_000
        response["Server-Timing"] = f"app;dur={elapsed:.6f}"
        if self.resources:
            response["X-Perf-Resources"] = self.resources
        return response
