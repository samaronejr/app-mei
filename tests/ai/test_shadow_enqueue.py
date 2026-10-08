"""Behavioral red proof uses the existing extraction entry point, not imports."""

import pytest
from celery import Task

from apps.cycles.tasks import extract_document
from tests.cycles.document_fixtures import scan_clean, submit
from tests.portal.request_fixtures import PortalSetup


@pytest.mark.django_db(transaction=True)
def test_real_extraction_enqueues_the_enabled_shadow(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[str] = []

    def capture(task: Task, *args: object, **kwargs: object) -> None:
        queued.append(str(task.name))

    monkeypatch.setattr(Task, "apply_async", capture)
    intake = submit(setup, "recibo_cliente.pdf")
    scan_clean(setup, intake)
    result = extract_document(str(setup.graph.tenant.pk), str(intake.pk))
    assert result == "classificado"
    # AXR1-017 also enqueues its receipt materializer once the upload commits.
    assert [name for name in queued if name.startswith("apps.automation.")] == [
        "apps.automation.tasks.classify_document"
    ]
