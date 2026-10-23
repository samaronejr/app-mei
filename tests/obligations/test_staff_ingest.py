"""New staff evidence is private by default without changing legacy row defaults."""

from pathlib import Path

import pytest
from django.http import Http404
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.intake import UploadRejected
from apps.obligations.models import Document
from tests.obligations.ingest_fixtures import PDF, ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup, upload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


def test_staff_intake_defaults_hidden_with_real_human_provenance(
    setup: PortalSetup,
) -> None:
    from apps.cycles.intake import staff_upload  # noqa: PLC0415

    # Given firm-side evidence with a real actor.
    with tenant_context(setup.graph.tenant.pk):
        # When staff use the ordinary intake without a visibility override.
        document, reused = staff_upload(
            setup.graph.actor,
            upload("authority.pdf", PDF),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
        )
    # Then no fabricated client submission or portal exposure is created.
    assert reused is False
    assert document.portal_visible is False
    assert document.client_upload is False
    assert document.acquisition_origin == "upload_equipe"
    assert document.acquisition_receipt_ref == ""
    assert document.uploaded_by_id == setup.graph.actor.pk
    with portal_cursor(setup) as cursor:
        cursor.execute(
            "SELECT id FROM obligations_document WHERE id = %s", [document.pk]
        )
        assert cursor.fetchall() == []


def test_staff_dedup_is_scoped_to_the_explicit_client_and_visibility(
    setup: PortalSetup,
) -> None:
    from apps.cycles.intake import staff_upload  # noqa: PLC0415

    # Given identical staff bytes for a sibling client and a hidden original.
    with tenant_context(setup.graph.tenant.pk):
        sibling, _ = staff_upload(
            setup.graph.actor,
            upload("sibling.pdf", PDF),
            tenant=setup.graph.tenant,
            client=setup.other_client,
        )
        hidden, _ = staff_upload(
            setup.graph.actor,
            upload("hidden.pdf", PDF),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
        )
        # When an explicitly visible staff upload is requested for this client.
        visible, reused = staff_upload(
            setup.graph.actor,
            upload("visible.pdf", PDF),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            portal_visible=True,
            acquisition_origin="upload_equipe",
        )
        # Then all three identities remain distinct and correctly attributed.
        assert len({sibling.pk, hidden.pk, visible.pk}) == 3
        assert reused is False
        assert visible.client_id == setup.graph.client.pk
        assert Document.objects.filter(client=setup.graph.client).count() == 2
    with portal_cursor(setup) as cursor:
        cursor.execute("SELECT id FROM obligations_document")
        assert cursor.fetchall() == [(visible.pk,)]


def test_unassigned_staff_cannot_ingest_for_an_invisible_client(
    setup: PortalSetup,
) -> None:
    from apps.cycles.intake import staff_upload  # noqa: PLC0415

    # Given an accountant with no assignment to this client.
    with tenant_context(setup.graph.tenant.pk):
        # When they try to upload evidence for a guessed client ID.
        with pytest.raises(Http404):
            staff_upload(
                setup.graph.staff,
                upload("private.pdf", PDF),
                tenant=setup.graph.tenant,
                client=setup.graph.client,
            )
        # Then the portfolio denial leaves no document or bytes.
        assert not Document.objects.exists()


@pytest.mark.parametrize(
    "origin", ["upload_portal", "provedor_federal", "captura_provedor", "unknown"]
)
def test_staff_intake_cannot_forge_another_acquisition_origin(
    setup: PortalSetup, origin: str
) -> None:
    from apps.cycles.intake import staff_upload  # noqa: PLC0415

    # Given a staff actor, not a trusted provider task.
    with tenant_context(setup.graph.tenant.pk):
        # When they supply another entry point's origin.
        with pytest.raises(UploadRejected):
            staff_upload(
                setup.graph.actor,
                upload("forged.pdf", PDF),
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                acquisition_origin=origin,
            )
        # Then the refused acquisition writes nothing.
        assert not Document.objects.exists()
