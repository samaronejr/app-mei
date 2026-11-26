"""Live ISS recovery must converge regardless of a retained-read outage."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from apps.core.tenancy import tenant_context
from apps.obligations.models import RevisionArtifact
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import nfse
from tests.obligations.iss_vault_fixture import fault_iss_original
from tests.obligations.test_fiscal_documents import (
    _STATED,
    _ingest_iss,
    _iss_projection,
)

if TYPE_CHECKING:
    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup


@pytest.mark.parametrize("outage", [False, True], ids=["healthy", "503-then-replay"])
def test_writer_owned_attachment_reprojects_after_outage(
    setup: PortalSetup, outage: bool
) -> None:
    clean = nfse()
    accepted = _ingest_iss(setup, "2")
    assert _iss_projection(accepted) == ("tomador", True, _STATED)
    revision = accepted.current_revision_id
    with tenant_context(accepted.tenant_id):
        original = RevisionArtifact.objects.get(revision_id=revision).document

    fault = (
        fault_iss_original("503", original.storage_key, clean)
        if outage
        else nullcontext()
    )
    with fault:
        attached = _ingest_iss(setup, " 2 ")
    if outage:
        assert attached.fiscal_state == "incerta"
        assert _iss_projection(attached) == ("desconhecido", True, _STATED)

    recovered = _ingest_iss(setup, " 2 ")
    assert recovered.fiscal_state == "autorizada"
    assert _iss_projection(recovered) == ("desconhecido", None, None)
    assert recovered.current_revision_id == revision
    if not outage:
        assert attached.version == recovered.version
        assert _iss_projection(attached) == _iss_projection(recovered)
    replay = _ingest_iss(setup, " 2 ")
    assert replay.version == recovered.version
    assert _iss_projection(replay) == _iss_projection(recovered)
    with tenant_context(replay.tenant_id):
        assert RevisionArtifact.objects.filter(revision_id=revision).count() == 2
