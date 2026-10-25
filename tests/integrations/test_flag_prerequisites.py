"""AXRM-007: the three regional flags are inert without `cycles_r1` and `scan_required`.

INV-R13 (plan A7.2): `document_capture`, `federal_checks` and `municipal_work` must
answer False for a tenant that lacks required scanning, whatever its row carries, so
no provider work can start on unscanned intake. The prerequisites live in the flag
registry itself (`apps.core.flags.FEATURE_PREREQUISITES`), so every way of asking --
`tenant_has`, `tenant_row_has`, `require_feature` -- gives the same answer.

`transaction=True` for the reason tests/core/test_flags.py documents.
"""

from typing import Final

import pytest
from django.http import Http404, HttpRequest, HttpResponse
from pytest_django.fixtures import SettingsWrapper

from apps.core.flags import (
    FEATURE_PREREQUISITES,
    FEATURES,
    require_feature,
    tenant_has,
    tenant_row_has,
)
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

REGIONAL_FLAGS: Final = ("federal_checks", "document_capture", "municipal_work")
PREREQUISITES: Final = frozenset({"cycles_r1", "scan_required"})


class _TenantRequest(HttpRequest):
    tenant: Tenant


def _tenant(slug: str, *flags: str) -> Tenant:
    return Tenant.objects.create(name=slug.title(), slug=slug, features=list(flags))


def test_the_regional_flags_are_registered_with_both_prerequisites() -> None:
    # Pinned: a regional flag that loses a prerequisite would let provider work
    # start for a firm whose intake is not scanned.
    for flag in REGIONAL_FLAGS:
        assert flag in FEATURES
        assert FEATURE_PREREQUISITES[flag] == PREREQUISITES
    assert FEATURE_PREREQUISITES["scan_required"] == frozenset({"cycles_r1"})
    # Every prerequisite is itself a registered flag, so a typo cannot hide here.
    for flag, required in FEATURE_PREREQUISITES.items():
        assert flag in FEATURES
        assert required <= FEATURES


@pytest.mark.parametrize("flag", REGIONAL_FLAGS)
def test_a_regional_flag_is_inert_without_scan_required(flag: str) -> None:
    tenant = _tenant(f"sem-scan-{flag.replace('_', '-')}", "cycles_r1", flag)

    assert tenant_has(tenant.pk, flag) is False
    assert tenant_row_has(tenant, flag) is False


@pytest.mark.parametrize("flag", REGIONAL_FLAGS)
def test_a_regional_flag_is_inert_without_cycles_r1(flag: str) -> None:
    tenant = _tenant(f"sem-ciclos-{flag.replace('_', '-')}", "scan_required", flag)

    assert tenant_has(tenant.pk, flag) is False
    assert tenant_row_has(tenant, flag) is False


@pytest.mark.parametrize("flag", REGIONAL_FLAGS)
def test_a_regional_flag_answers_true_only_with_both_prerequisites(flag: str) -> None:
    tenant = _tenant(
        f"completo-{flag.replace('_', '-')}", "cycles_r1", "scan_required", flag
    )
    bare = _tenant(f"so-flag-{flag.replace('_', '-')}", flag)

    # The positive control: without it every False above could be a broken flag.
    assert tenant_has(tenant.pk, flag) is True
    assert tenant_row_has(tenant, flag) is True
    assert tenant_has(bare.pk, flag) is False


@pytest.mark.parametrize("flag", REGIONAL_FLAGS)
def test_killing_a_prerequisite_platform_wide_stands_the_regional_flag_down(
    flag: str,
    settings: SettingsWrapper,
) -> None:
    tenant = _tenant(
        f"kill-{flag.replace('_', '-')}", "cycles_r1", "scan_required", flag
    )
    settings.AXORIM_DISABLED_FEATURES = ["scan_required"]

    assert tenant_has(tenant.pk, flag) is False
    assert tenant_row_has(tenant, flag) is False


def test_a_view_gated_on_a_regional_flag_404s_while_scanning_is_not_required() -> None:
    tenant = _tenant("gate-scan", "cycles_r1", "federal_checks")

    @require_feature("federal_checks")
    def view(_request: _TenantRequest) -> HttpResponse:
        return HttpResponse("ok")

    request = _TenantRequest()
    request.tenant = tenant
    with pytest.raises(Http404):
        view(request)

    tenant.features = [*tenant.features, "scan_required"]
    tenant.save(update_fields=["features"])
    assert view(request).status_code == 200


def test_scan_required_keeps_its_cycles_r1_prerequisite() -> None:
    tenant = _tenant("scan-sem-ciclos", "scan_required")

    assert tenant_has(tenant.pk, "scan_required") is False
    tenant.features = ["cycles_r1", "scan_required"]
    tenant.save(update_fields=["features"])
    assert tenant_has(tenant.pk, "scan_required") is True
