"""Bulk selection, reports and effects all retain tenant and portfolio boundaries."""

import csv
import io

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from apps.authz.models import GrantLevel, RoleGrant
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState, BulkKind, BulkOperation
from apps.cycles.bulk_contracts import Selection
from apps.cycles.models import ServiceCycle
from apps.cycles.services.bulk import bulk_report_csv, preview_bulk
from apps.cycles.tasks import run_bulk_operation
from apps.tenants.models import TenantRole
from tests.cycles.bulk_fixtures import bulk_setup, confirm, item_states
from tests.cycles.cycle_fixtures import PERIOD

pytestmark = pytest.mark.django_db(transaction=True)


def test_unassigned_accountant_cannot_preview_even_with_bulk_capability() -> None:
    setup = bulk_setup("bulk-unassigned")
    RoleGrant.objects.filter(
        role=TenantRole.STAFF_ACCOUNTANT, capability__slug="bulk.run"
    ).update(level=GrantLevel.FULL)
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(Http404):
            preview_bulk(
                setup.graph.staff,
                BulkKind.OPEN_CYCLES,
                Selection(
                    (setup.graph.engagement.pk,),
                    period=PERIOD,
                    playbook_version_id=setup.graph.definition.pk,
                ),
            )
        assert not BulkOperation.objects.exists()
        assert not ServiceCycle.objects.exists()
    assert setup.preview().snapshot_json["items"][0]["eligible"] is True


def test_other_tenant_cannot_load_snapshot_or_expand_a_foreign_subject() -> None:
    own = bulk_setup("bulk-scope-a", count=1)
    foreign = bulk_setup("bulk-scope-b", count=1)
    operation = confirm(own.graph.actor, own.preview())
    with tenant_context(foreign.graph.tenant.pk):
        with pytest.raises(Http404):
            bulk_report_csv(foreign.graph.actor, operation.pk)
        with pytest.raises(Http404):
            preview_bulk(
                foreign.graph.actor,
                BulkKind.OPEN_CYCLES,
                Selection(
                    (own.graph.engagement.pk,),
                    period=PERIOD,
                    playbook_version_id=foreign.graph.definition.pk,
                ),
            )
        assert not BulkOperation.objects.exists()
    with tenant_context(own.graph.tenant.pk):
        assert bulk_report_csv(own.graph.actor, operation.pk).status_code == 200


def test_revoked_bulk_authority_is_checked_again_for_each_item() -> None:
    setup = bulk_setup("bulk-role-revoked")
    operation = confirm(setup.graph.actor, setup.preview())
    RoleGrant.objects.filter(role=TenantRole.OWNER, capability__slug="bulk.run").update(
        level=GrantLevel.NONE
    )
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        assert not ServiceCycle.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.SKIPPED_FORBIDDEN}
        with pytest.raises(PermissionDenied):
            bulk_report_csv(setup.graph.actor, operation.pk)


def test_flag_off_prevents_preview_and_pauses_already_queued_work() -> None:
    setup = bulk_setup("bulk-flag-off", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    setup.graph.tenant.features = ["cycles_r1"]
    setup.graph.tenant.save(update_fields=["features"])
    with pytest.raises(Http404):
        setup.preview()
    assert run_bulk_operation(str(operation.tenant_id), str(operation.pk)) == 0
    with tenant_context(operation.tenant_id):
        assert not ServiceCycle.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.PENDING}


def test_empty_and_duplicate_selections_do_not_execute() -> None:
    setup = bulk_setup("bulk-selection", count=1)
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(ValidationError):
            preview_bulk(
                setup.graph.actor,
                BulkKind.OPEN_CYCLES,
                Selection(
                    (setup.graph.engagement.pk, setup.graph.engagement.pk),
                    period=PERIOD,
                    playbook_version_id=setup.graph.definition.pk,
                ),
            )
        empty = preview_bulk(
            setup.graph.actor,
            BulkKind.OPEN_CYCLES,
            Selection(
                (),
                period=PERIOD,
                playbook_version_id=setup.graph.definition.pk,
            ),
        )
        assert empty.snapshot_json["items"] == []
        with pytest.raises(ValidationError):
            confirm(setup.graph.actor, empty)
        assert not ServiceCycle.objects.exists()


def test_report_csv_defuses_a_frozen_formula_and_is_not_streaming() -> None:
    setup = bulk_setup("bulk-csv", count=1)
    with tenant_context(setup.graph.tenant.pk):
        ClientCompany.objects.filter(pk=setup.graph.client.pk).update(
            legal_name='=HYPERLINK("https://example.invalid")'
        )
    operation = confirm(setup.graph.actor, setup.preview())
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        response = bulk_report_csv(setup.graph.actor, operation.pk)
    assert response.streaming is False
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["cliente"] == '\'=HYPERLINK("https://example.invalid")'
    assert rows[0]["estado"] == BulkItemState.DONE
