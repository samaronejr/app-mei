"""The real upload/scan/extraction path must never enqueue unsafe documents."""

import pytest
from celery.exceptions import OperationalError
from pytest_django.fixtures import SettingsWrapper

from apps.automation.models import AgentRun
from apps.automation.shadow import run_shadow
from apps.automation.tasks import classify_document, enqueue_shadow
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink, IntakeState, ScanVerdict
from apps.cycles.tasks import extract_document
from tests.ai.fixtures import single_company
from tests.cycles.document_fixtures import reviewed, scan_clean, submit
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)


def test_classified_clean_link_is_queued(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a clean, linked upload and enabled shadow feature.
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(classify_document, "delay", lambda *args: queued.append(args))
    intake = submit(setup, "recibo_cliente.pdf")
    scan_clean(setup, intake)
    # When the deterministic task finishes.
    result = extract_document(str(setup.graph.tenant.pk), str(intake.pk))
    # Then a shadow task is enqueued only after its deterministic commit.
    assert result == "classificado"
    assert queued == [(str(setup.graph.tenant.pk), str(intake.pk))]


@pytest.mark.parametrize(
    ("name", "scan", "outcome"),
    [
        ("recibo_cliente.pdf", False, "skipped"),
        ("nfe_outra_empresa.xml", True, "divergente"),
        ("recibo_outra_empresa.pdf", True, "divergente"),
    ],
)
def test_divergent_or_quarantined_never_enqueued(
    setup: PortalSetup,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    scan: bool,
    outcome: str,
) -> None:
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(classify_document, "delay", lambda *args: queued.append(args))
    intake = submit(setup, name)
    if scan:
        scan_clean(setup, intake)
    assert extract_document(str(setup.graph.tenant.pk), str(intake.pk)) == outcome
    assert queued == []


def test_state_is_rechecked_at_worker_admission(setup: PortalSetup) -> None:
    intake, _ = reviewed(setup, "nfe_outra_empresa.xml")
    assert intake.state == IntakeState.DIVERGENT
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "skipped"
    with tenant_context(setup.graph.tenant.pk):
        assert AgentRun.objects.count() == 0


def test_broker_failure_does_not_change_extraction(
    setup: PortalSetup,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    def unavailable(*args: str) -> None:
        message = "offline"
        raise OperationalError(message)

    monkeypatch.setattr(classify_document, "delay", unavailable)
    intake = submit(setup, "recibo_cliente.pdf")
    scan_clean(setup, intake)
    assert (
        extract_document(str(setup.graph.tenant.pk), str(intake.pk)) == "classificado"
    )
    with tenant_context(setup.graph.tenant.pk):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.CLASSIFIED


@pytest.mark.parametrize(
    "condition", ["manual_classified", "manual_linked", "unlinked"]
)
def test_clean_verdict_and_actual_link_are_independent_requirements(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    intake = single_company(setup)
    with tenant_context(setup.graph.tenant.pk):
        if condition == "unlinked":
            EvidenceLink.objects.filter(document_id=intake.document_id).delete()
        else:
            DocumentIntake.objects.filter(pk=intake.pk).update(
                state=(
                    IntakeState.LINKED
                    if condition == "manual_linked"
                    else IntakeState.CLASSIFIED
                ),
                verdict=ScanVerdict.UNAVAILABLE,
            )

    def unexpected(*args: object) -> None:
        pytest.fail("unscanned or unlinked intake reached broker")

    monkeypatch.setattr(classify_document, "delay", unexpected)
    assert enqueue_shadow(setup.graph.tenant.pk, intake.pk) is False
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "skipped"
    with tenant_context(setup.graph.tenant.pk):
        assert AgentRun.objects.count() == 0
