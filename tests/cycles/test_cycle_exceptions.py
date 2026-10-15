"""Registered openers converge, preserve ownership and refuse stale/unscoped writes."""

from functools import partial

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404

from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.services import exceptions
from apps.cycles.services.cycle import completion_unmet, open_cycle
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

pytestmark = pytest.mark.django_db


def test_exception_registry_idempotence_scope_and_stale_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = cycle_setup()
    monkeypatch.setattr(exceptions, "_REASONS", {})
    exceptions.register_exception_reason(
        "document-missing", exceptions.ExceptionReason("Solicitar a evidência")
    )
    with pytest.raises(ValueError, match="document-missing"):
        exceptions.register_exception_reason(
            "document-missing", exceptions.ExceptionReason("Duplicate")
        )
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="exception",
        )
        open_problem = partial(
            exceptions.open_exception,
            cycle_id=ref.id,
            expected_version=1,
            owner=setup.actor,
            review_on=PERIOD,
        )
        with pytest.raises(Http404):
            open_problem("document-missing", setup.client.pk, actor=setup.staff)
        assert not CycleException.objects.exists()
        problem = open_problem("document-missing", setup.client.pk, actor=setup.actor)
        replay = open_problem("document-missing", setup.client.pk, actor=setup.actor)
        assert problem.pk == replay.pk
        assert CycleEvent.objects.count() == 2
        with pytest.raises(VersionConflict):
            open_problem("document-missing", setup.engagement.pk, actor=setup.actor)
        assert CycleException.objects.count() == 1
        assert "exceptions" in completion_unmet(ServiceCycle.objects.get(pk=ref.id))
        with pytest.raises(ValidationError):
            open_problem("unknown-reason", setup.client.pk, actor=setup.actor)
