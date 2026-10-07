"""Frozen request audiences and independent exact-effect approval decisions."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState, BulkKind
from apps.cycles.bulk_contracts import Selection
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import Approval, RequestState
from apps.cycles.services.approvals import propose
from apps.cycles.services.bulk import preview_bulk
from apps.cycles.services.requests import RequestFields, create_request, update_request
from apps.cycles.tasks import run_bulk_operation
from apps.delivery.models import DeliveryAttempt, DeliveryEffect, OutboundMessage
from apps.tenants.models import TenantRole
from tests.cycles.approval_fixtures import approval_graph
from tests.cycles.bulk_fixtures import confirm, item_states
from tests.portal.request_fixtures import portal_setup
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


def test_request_notice_uses_only_frozen_recipients_and_never_sends_smtp() -> None:
    setup = portal_setup("bulk-request-notice")
    setup.graph.tenant.features = ["cycles_r1", "bulk_r1"]
    setup.graph.tenant.save(update_fields=["features"])
    with tenant_context(setup.graph.tenant.pk):
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields(title="Extrato adicional"),
            expected_version=setup.cycle.version,
        )
        preview = preview_bulk(
            setup.graph.actor,
            BulkKind.SEND_REQUEST_NOTICES,
            Selection((setup.cycle.pk,)),
        )
        original_ids = set(OutboundMessage.objects.values_list("pk", flat=True))
        attempt_ids = set(DeliveryAttempt.objects.values_list("pk", flat=True))
    operation = confirm(setup.graph.actor, preview)
    # This new authorized contact is not one of the recipients the user previewed.
    new_user = add_member(
        setup.graph.tenant,
        "later@bulk.test",
        TenantRole.CLIENT_OWNER,
        client=setup.graph.client,
    )
    with tenant_context(setup.graph.tenant.pk):
        ClientContact.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            name="Contato posterior",
            email=new_user.email,
        )
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        request.refresh_from_db()
        assert request.state == RequestState.REQUESTED
        messages = OutboundMessage.objects.exclude(pk__in=original_ids)
        assert list(messages.values_list("recipients_json", flat=True)) == [
            [setup.contact.email]
        ]
        assert list(messages.values_list("state", flat=True)) == ["na_fila"]
        assert DeliveryAttempt.objects.exclude(pk__in=attempt_ids).count() == 1
        assert not DeliveryEffect.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.DONE}


def test_request_edit_without_parent_revision_change_is_not_silently_sent() -> None:
    setup = portal_setup("bulk-request-edited")
    setup.graph.tenant.features = ["cycles_r1", "bulk_r1"]
    setup.graph.tenant.save(update_fields=["features"])
    with tenant_context(setup.graph.tenant.pk):
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields(title="Texto conferido"),
            expected_version=setup.cycle.version,
        )
        preview = preview_bulk(
            setup.graph.actor,
            BulkKind.SEND_REQUEST_NOTICES,
            Selection((setup.cycle.pk,)),
        )
        original = set(OutboundMessage.objects.values_list("pk", flat=True))
    operation = confirm(setup.graph.actor, preview)
    with tenant_context(operation.tenant_id):
        update_request(
            setup.graph.actor,
            request.pk,
            RequestFields(title="Texto alterado depois da prévia"),
            expected_version=request.version,
        )
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        request.refresh_from_db()
        assert request.state == RequestState.DRAFT
        assert set(OutboundMessage.objects.values_list("pk", flat=True)) == original
        assert set(item_states(operation).values()) == {BulkItemState.SKIPPED_CHANGED}


def test_each_approval_delegates_its_own_frozen_digest_without_executing() -> None:
    setup = approval_graph("bulk-approve-digests")
    graph = setup.graph
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    with tenant_context(graph.tenant.pk):
        second = propose(
            graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="different-notice",
                    recipient_ids=(setup.contact.pk,),
                    subject="Outro assunto",
                    body="Um efeito materialmente diferente.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        assert second.digest != setup.proposal.digest
        preview = preview_bulk(
            graph.actor,
            BulkKind.APPROVE_ITEMS,
            Selection((setup.proposal.pk, second.pk)),
        )
        effects = [row["effect"] for row in preview.snapshot_json["items"]]
        assert [row["proposal_digest"] for row in effects] == [
            setup.proposal.digest,
            second.digest,
        ]
    operation = confirm(graph.actor, preview)
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        assert set(Approval.objects.values_list("proposal_id", "digest")) == {
            (setup.proposal.pk, setup.proposal.digest),
            (second.pk, second.digest),
        }
        assert not OutboundMessage.objects.exists()
        assert not DeliveryEffect.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.DONE}


def test_changed_approval_source_uses_t15_invalidation_not_a_cached_allow() -> None:
    setup = approval_graph("bulk-approve-changed")
    graph = setup.graph
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    with tenant_context(graph.tenant.pk):
        preview = preview_bulk(
            graph.actor, BulkKind.APPROVE_ITEMS, Selection((setup.proposal.pk,))
        )
    operation = confirm(graph.actor, preview)
    with tenant_context(graph.tenant.pk):
        ClientContact.objects.filter(pk=setup.contact.pk).update(
            email="different@example.test"
        )
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        assert not Approval.objects.exists()
        assert not OutboundMessage.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.SKIPPED_CHANGED}
