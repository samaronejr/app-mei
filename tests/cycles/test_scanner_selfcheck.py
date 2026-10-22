"""AXRM-005: `manage.py scanner_selfcheck --tenant <synthetic>` (operator gate OG-R1).

It sends an EICAR-carrying PDF and a clean PDF through the real vault intake
(`vault_upload`: sniff -> quarantine -> intake) of a tenant that carries
`scan_required`, scans both through the configured clamd, prints each verdict and
fails unless EICAR is `rejeitado_malware` and the clean file `verificado`. It refuses
a tenant without the flag, and a host with scanning switched off. Here clamd is
`tests.fakes.clamd`; on the host it is the real sidecar.
"""

from io import StringIO
from pathlib import Path

import pytest
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.obligations.models import Document
from tests.fakes.clamd import FakeClamd, fake_clamd
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = True
    settings.CLAMD_HOST = "127.0.0.1"
    return portal_setup("axrm005-selfcheck")


def _flag(setup: PortalSetup) -> None:
    tenant = setup.graph.tenant
    tenant.features = [*tenant.features, "scan_required"]
    tenant.save(update_fields=["features"])


def _run(setup: PortalSetup) -> str:
    out = StringIO()
    call_command("scanner_selfcheck", "--tenant", setup.graph.tenant.slug, stdout=out)
    return out.getvalue()


def _intakes(setup: PortalSetup) -> dict[str, str]:
    with tenant_context(setup.graph.tenant.pk):
        return dict(
            DocumentIntake.objects.values_list("document__original_filename", "state")
        )


def _documents(setup: PortalSetup) -> int:
    with tenant_context(setup.graph.tenant.pk):
        return Document.objects.count()


def _use(settings: SettingsWrapper, server: FakeClamd) -> None:
    settings.CLAMD_PORT = server.port


def test_eicar_is_rejected_and_the_clean_pdf_verified_through_the_real_intake(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup)
    with fake_clamd() as server:
        _use(settings, server)
        report = _run(setup)
        scanned = list(server.scanned)

    assert "eicar: rejeitado_malware malware:Eicar-Signature PASS" in report
    assert "limpo: verificado PASS" in report
    assert report.rstrip().endswith("RESULT PASS")
    assert "signatures=28150/Sun Oct 11 06:26:01 2026" in report
    assert _intakes(setup) == {
        "scanner-selfcheck-eicar.pdf": IntakeState.REJECTED_MALWARE,
        "scanner-selfcheck-limpo.pdf": IntakeState.VERIFIED,
    }
    assert len(scanned) == 2, "both files went through clamd INSTREAM"
    with tenant_context(setup.graph.tenant.pk):
        clean = Document.objects.get(original_filename="scanner-selfcheck-limpo.pdf")
        eicar = DocumentIntake.objects.select_related("document").get(
            document__original_filename="scanner-selfcheck-eicar.pdf"
        )
    assert default_storage.exists(clean.storage_key)
    assert default_storage.exists(eicar.quarantine_key), "the EICAR copy is retained"
    assert not default_storage.exists(eicar.document.storage_key)


def test_a_second_run_uses_fresh_bytes_and_passes_again(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup)
    with fake_clamd() as server:
        _use(settings, server)
        _run(setup)
        assert _run(setup).rstrip().endswith("RESULT PASS")
    assert _documents(setup) == 4


def test_a_scanner_that_misses_eicar_fails_the_selfcheck(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup)
    out = StringIO()
    with fake_clamd(detect=False) as server:
        _use(settings, server)
        with pytest.raises(CommandError, match="RESULT FAIL"):
            call_command(
                "scanner_selfcheck", "--tenant", setup.graph.tenant.slug, stdout=out
            )
    assert "eicar: verificado FAIL" in out.getvalue()


def test_an_unreachable_scanner_fails_before_any_upload(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup)
    with fake_clamd(silent=True) as hung:
        _use(settings, hung)
        settings.CLAMD_TIMEOUT_SECONDS = 0.2
        with pytest.raises(CommandError, match="clamd"):
            _run(setup)
    assert _documents(setup) == 0


def test_a_tenant_without_scan_required_is_refused_and_nothing_is_written(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    with fake_clamd() as server:
        _use(settings, server)
        with pytest.raises(CommandError, match="scan_required"):
            _run(setup)
        assert server.commands == []
    assert _documents(setup) == 0


def test_scanning_switched_off_is_refused(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup)
    settings.CLAMAV_ENABLED = False
    with pytest.raises(CommandError, match="CLAMAV_ENABLED"):
        _run(setup)
    assert _documents(setup) == 0


def test_an_unknown_tenant_is_refused(setup: PortalSetup) -> None:
    with pytest.raises(CommandError, match="no tenant"):
        call_command("scanner_selfcheck", "--tenant", "nao-existe", stdout=StringIO())
    assert _documents(setup) == 0
