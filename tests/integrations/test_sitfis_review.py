"""An accountant reviews the scanned SITFIS report; pendencias become owned work.

AXRM-016 acceptance (plan todo 16): `FederalReportReview` is append-only, needs a
clean scan of the exact bytes, follows the locked/versioned/can()/event recipe,
and only `sem_pendencias_nas_fontes` gets the dated "no pendencia" sentence.
"""

import json
from datetime import timedelta
from types import ModuleType
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.obligations.models import FiscalSituationReport
from apps.tenants.models import Membership, TenantRole
from tests.integrations.sitfis_support import (
    World,
    clock,
    module,
    protocol_reply,
    report_reply,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]

PENDENCIA = "pendencia_fiscal_federal"


def review_module() -> ModuleType:
    return module("apps.cycles.services.federal_review")


def _emitted(world: World) -> FiscalSituationReport:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    emitted = world.advance(report)
    assert emitted.state == "emitido"
    return emitted


def _cycle_version(world: World) -> int:
    with tenant_context(world.tenant_id):
        return ServiceCycle.objects.get(pk=world.cycle.pk).version


def _review(  # noqa: PLR0913 - the review form's fields plus the actor
    world: World,
    report: FiscalSituationReport,
    *,
    actor: User | None = None,
    outcome: str = "sem_pendencias_nas_fontes",
    pendencias: tuple[str, ...] = (),
    expected_version: int | None = None,
    digest: str | None = None,
) -> Any:  # noqa: ANN401 - the model arrives with AXRM-016
    reviews = review_module()
    with tenant_context(world.tenant_id):
        current = FiscalSituationReport.objects.get(pk=report.pk)
        return reviews.record_federal_report_review(
            actor or world.responsible,
            world.cycle.pk,
            current.pk,
            outcome=outcome,
            pendencias=list(pendencias),
            expected_version=(
                _cycle_version(world) if expected_version is None else expected_version
            ),
            input_digest=(
                reviews.report_input_digest(current) if digest is None else digest
            ),
        )


def _reviews(world: World) -> list[Any]:
    from apps.cycles import models  # noqa: PLC0415

    assert hasattr(models, "FederalReportReview"), (
        "AXRM-016 must register cycles.FederalReportReview"
    )
    with tenant_context(world.tenant_id):
        return list(models.FederalReportReview.objects.order_by("reviewed_at"))


def test_a_report_cannot_be_reviewed_before_its_scan(world: World) -> None:
    report = _emitted(world)
    with pytest.raises(ValidationError):
        _review(world, report)
    assert _reviews(world) == []
    with tenant_context(world.tenant_id):
        assert not CycleEvent.objects.filter(type="federal.report.reviewed").exists()


def test_no_pendencias_review_records_and_uses_the_dated_copy(world: World) -> None:
    report = _emitted(world)
    world.scan()
    before = _cycle_version(world)
    review = _review(world, report)
    [stored] = _reviews(world)
    assert stored.pk == review.pk
    assert (stored.report_id, stored.cycle_id, stored.reviewer_id) == (
        report.pk,
        world.cycle.pk,
        world.responsible.pk,
    )
    assert (stored.outcome, stored.pendencias) == ("sem_pendencias_nas_fontes", [])
    with tenant_context(world.tenant_id):
        current = FiscalSituationReport.objects.get(pk=report.pk)
        assert stored.input_digest == review_module().report_input_digest(current)
        [event] = CycleEvent.objects.filter(type="federal.report.reviewed")
    assert _cycle_version(world) == before + 1
    assert event.actor_id == world.responsible.pk
    assert event.payload["outcome"] == "sem_pendencias_nas_fontes"
    statement = review_module().review_statement(stored, report)
    emitted_on = timezone.localtime(report.emitted_at).strftime("%d/%m/%Y")
    assert statement is not None
    assert emitted_on in statement
    assert not _exceptions(world)


def test_pendencias_open_one_owned_exception_each(world: World) -> None:
    report = _emitted(world)
    world.scan()
    items = ("Debito em aberto no sistema sintetico A", "Omissao sintetica B")
    _review(world, report, outcome="pendencias_registradas", pendencias=items)
    [stored] = _reviews(world)
    assert stored.pendencias == list(items)
    problems = _exceptions(world)
    assert len(problems) == 2
    assert {problem.owner_id for problem in problems} == {world.responsible.pk}
    assert len({problem.subject_ref for problem in problems}) == 2
    assert all(problem.cycle_id == world.cycle.pk for problem in problems)
    assert review_module().review_statement(stored, report) is None
    with tenant_context(world.tenant_id):
        [event] = CycleEvent.objects.filter(type="federal.report.reviewed")
    # The firm's prose stays in the firm-only review row, never in the envelope.
    assert all(item not in json.dumps(event.payload) for item in items)
    assert event.payload["pendencias"] == 2


def test_an_unreadable_report_review_has_no_assurance_copy(world: World) -> None:
    report = _emitted(world)
    world.scan()
    _review(world, report, outcome="ilegivel")
    [stored] = _reviews(world)
    assert review_module().review_statement(stored, report) is None
    assert not _exceptions(world)


@pytest.mark.parametrize(
    ("outcome", "pendencias"),
    [
        ("pendencias_registradas", ()),
        ("pendencias_registradas", ("   ",)),
        ("sem_pendencias_nas_fontes", ("algo",)),
        ("ilegivel", ("algo",)),
        ("regular", ()),
        ("pendencias_registradas", ("x" * 501,)),
    ],
)
def test_outcome_and_pendencias_must_agree(
    world: World, outcome: str, pendencias: tuple[str, ...]
) -> None:
    report = _emitted(world)
    world.scan()
    with pytest.raises(ValidationError):
        _review(world, report, outcome=outcome, pendencias=pendencias)
    assert _reviews(world) == []
    assert not _exceptions(world)


def test_a_stale_caller_conflicts_without_writes(world: World) -> None:
    report = _emitted(world)
    world.scan()
    version = _cycle_version(world)
    _review(world, report, expected_version=version)
    with pytest.raises(VersionConflict):
        _review(
            world,
            report,
            outcome="pendencias_registradas",
            pendencias=("Pendencia sintetica",),
            expected_version=version,
        )
    assert len(_reviews(world)) == 1
    assert not _exceptions(world)


def test_a_review_of_other_bytes_conflicts_without_writes(world: World) -> None:
    report = _emitted(world)
    world.scan()
    with pytest.raises(VersionConflict):
        _review(world, report, digest="0" * 64)
    assert _reviews(world) == []


def test_an_unassigned_accountant_is_denied_without_writes(world: World) -> None:
    report = _emitted(world)
    world.scan()
    outsider = User.objects.create_user(email="fora@axrm016.example.test")
    # PLATFORM_QUERY_OK: a staff member of the firm without this client.
    Membership.objects.create(
        tenant=world.termo.tenant, user=outsider, role=TenantRole.STAFF_ACCOUNTANT
    )
    with pytest.raises((Http404, PermissionDenied)):
        _review(world, report, actor=outsider)
    assert _reviews(world) == []


def test_a_report_that_is_not_emitted_cannot_be_reviewed(world: World) -> None:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    with tenant_context(world.tenant_id):
        current = FiscalSituationReport.objects.get(pk=report.pk)
        with pytest.raises(ValidationError):
            review_module().record_federal_report_review(
                world.responsible,
                world.cycle.pk,
                current.pk,
                outcome="sem_pendencias_nas_fontes",
                pendencias=[],
                expected_version=_cycle_version(world),
                input_digest="0" * 64,
            )
    assert _reviews(world) == []


def test_a_second_review_appends_and_never_rewrites_the_first(world: World) -> None:
    report = _emitted(world)
    world.scan()
    _review(world, report, outcome="ilegivel")
    world.clock.advance(minutes=5)
    _review(
        world,
        report,
        outcome="pendencias_registradas",
        pendencias=("Pendencia sintetica",),
    )
    first, second = _reviews(world)
    assert (first.outcome, second.outcome) == ("ilegivel", "pendencias_registradas")
    assert second.reviewed_at - first.reviewed_at == timedelta(minutes=5)
    assert len(_exceptions(world)) == 1


def _exceptions(world: World) -> list[CycleException]:
    with tenant_context(world.tenant_id):
        return list(CycleException.objects.filter(reason_code=PENDENCIA))
