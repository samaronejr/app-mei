"""A recorded dismissal is personal and names exactly the source fact it hides."""

from datetime import timedelta
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db.models import F
from django.http import Http404

from apps.audit.models import Event
from apps.briefing.feedback import Dismissal, dismiss
from apps.briefing.queries import briefing
from apps.briefing.types import BriefingFilters, BriefingItem
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from apps.cycles.models import (
    ActionProposal,
    BriefingFeedback,
    DocumentIntake,
    EvidenceRequest,
)
from apps.cycles.models import Exception as CycleException
from apps.delivery.models import OutboundMessage
from apps.obligations.models import (
    Obligation,
    ObservationVerificationStatus,
    SourceObservation,
)
from apps.obligations.observations import assert_fact
from tests.briefing.fixtures import NOW, briefing_setup, due_obligation
from tests.briefing.provider_fixtures import ProviderGraph, provider_graph
from tests.cycles.cycle_fixtures import PERIOD

pytestmark = pytest.mark.django_db(transaction=True)


def _change_fact(graph: ProviderGraph, item: BriefingItem) -> None:
    if item.kind == "obligation":
        Obligation.objects.filter(pk=item.subject_ref).update(
            resolved_due_date=NOW.date() + timedelta(days=1)
        )
    elif item.kind == "coverage":
        previous = SourceObservation.objects.get(pk=UUID(item.evidence_refs[0]))
        assert_fact(
            tenant_id=graph.setup.tenant.pk,
            client_id=graph.setup.client.pk,
            subject_id=graph.subjects["obligation"],
            kind="payment",
            period=PERIOD,
            source_kind="official_api",
            observed_at=NOW,
            value={},
            verification_status=ObservationVerificationStatus.STALE,
            outcome="corrected",
            supersedes=previous,
        )
    else:
        models = {
            "request": EvidenceRequest,
            "exception": CycleException,
            "approval": ActionProposal,
            "delivery": OutboundMessage,
            "document": DocumentIntake,
        }
        models[item.kind].objects.filter(pk=item.subject_ref).update(
            version=F("version") + 1
        )


@pytest.mark.parametrize(
    "kind",
    [
        "obligation",
        "request",
        "exception",
        "approval",
        "delivery",
        "document",
        "coverage",
    ],
)
def test_a_new_fact_version_resurfaces_each_dismissed_provider(kind: str) -> None:
    graph = provider_graph(briefing_setup())
    setup = graph.setup
    with tenant_context(setup.tenant.pk):
        page = briefing(setup.actor, scope="team", filters=BriefingFilters())
        item = next(row for row in page.items if row.kind == kind)
        command = Dismissal(
            kind,
            item.subject_ref,
            item.fact_version,
            "Já discutido com a equipe",
            item.period,
        )
        stored = dismiss(setup.actor, command)
        assert stored.reason == command.reason
        assert stored.fact_version == item.fact_version
        hidden = briefing(setup.actor, scope="team", filters=BriefingFilters())
        assert item.id not in {row.id for row in hidden.items}
        assert Event.objects.filter(object_type="BriefingFeedback").count() == 1

        _change_fact(graph, item)
        refreshed = briefing(setup.actor, scope="team", filters=BriefingFilters())

    current = next(row for row in refreshed.items if row.id == item.id)
    assert current.fact_version != item.fact_version


def test_an_old_form_cannot_dismiss_a_newer_fact_or_write_an_event() -> None:
    setup = briefing_setup()
    subject = due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        item = briefing(setup.actor, scope="team", filters=BriefingFilters()).items[0]
        Obligation.objects.filter(pk=subject.pk).update(
            resolved_due_date=NOW.date() + timedelta(days=1)
        )
        with pytest.raises(VersionConflict):
            dismiss(
                setup.actor,
                Dismissal("obligation", subject.pk, item.fact_version, "Já conferido"),
            )
        assert not BriefingFeedback.objects.exists()
        assert not Event.objects.filter(object_type="BriefingFeedback").exists()


def test_two_callers_cannot_record_the_same_dismissal_twice() -> None:
    setup = briefing_setup()
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        item = briefing(setup.actor, scope="team", filters=BriefingFilters()).items[0]
        command = Dismissal(
            item.kind, item.subject_ref, item.fact_version, "Já conferido"
        )
        dismiss(setup.actor, command)
        with pytest.raises(VersionConflict):
            dismiss(setup.actor, command)
        assert BriefingFeedback.objects.count() == 1
        assert Event.objects.filter(object_type="BriefingFeedback").count() == 1


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
def test_a_dismissal_requires_a_bounded_nonempty_reason(reason: str) -> None:
    setup = briefing_setup()
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        item = briefing(setup.actor, scope="team", filters=BriefingFilters()).items[0]
        with pytest.raises(ValidationError):
            dismiss(
                setup.actor,
                Dismissal(item.kind, item.subject_ref, item.fact_version, reason),
            )
        assert not BriefingFeedback.objects.exists()


def test_unassigned_staff_cannot_hide_a_known_subject_or_write_feedback() -> None:
    setup = briefing_setup()
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        control = briefing(setup.actor, scope="team", filters=BriefingFilters())
        item = control.items[0]
        with pytest.raises(Http404):
            dismiss(
                setup.staff,
                Dismissal(
                    item.kind, item.subject_ref, item.fact_version, "Não relevante"
                ),
            )
        assert not BriefingFeedback.objects.exists()
        assert not Event.objects.filter(object_type="BriefingFeedback").exists()


def test_dismissal_hides_only_the_person_who_recorded_it() -> None:
    setup = briefing_setup()
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        item = briefing(setup.actor, scope="team", filters=BriefingFilters()).items[0]
        dismiss(
            setup.actor,
            Dismissal(item.kind, item.subject_ref, item.fact_version, "Conferido"),
        )
        assert briefing(setup.actor, scope="team", filters=BriefingFilters()).total == 0

        colleague = briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert colleague.total == 1
    assert colleague.items[0].id == item.id
