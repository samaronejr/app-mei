"""The national received-service feed alone can request completa=1."""

from contextlib import suppress

import pytest

from apps.integrations.allowlist import resolve
from apps.integrations.transport_types import OperacaoNaoPermitida, ProviderRequest
from tests.obligations.fiscal_fixtures import CLIENT


def test_national_received_list_accepts_its_documented_complete_projection() -> None:
    request = ProviderRequest(
        query=(("cnpj", CLIENT), ("versao", "480"), ("completa", "1"))
    )
    resolved = None
    with suppress(OperacaoNaoPermitida):
        resolved = resolve("focus", "FC-NFS", request)
    assert resolved is not None, "FC-NFS lists must request completa=1."
    route, path = resolved
    assert route.method == "GET"
    assert path == f"/v2/nfsens_recebidas?cnpj={CLIENT}&versao=480&completa=1"


@pytest.mark.parametrize(
    ("operation", "value"),
    [("FC-NFE", "1"), ("FC-EMP", "1"), ("FC-NFS", "2"), ("FC-NFS", "true")],
)
def test_complete_projection_does_not_widen_other_routes_or_values(
    operation: str, value: str
) -> None:
    request = ProviderRequest(query=(("cnpj", CLIENT), ("completa", value)))
    allowed = True
    try:
        resolve("focus", operation, request)
    except OperacaoNaoPermitida:
        allowed = False
    assert allowed is False
