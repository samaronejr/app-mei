"""Closed internal handoff of a finalized termo token into broker process memory."""

import hashlib
import hmac
import json
import os
from typing import Final

from requests import Request

from apps.core.tasks import assert_no_transaction
from apps.integrations.serpro.termo_response import TermoToken
from apps.integrations.transport_types import TransportFailure
from apps.integrations.transport_wire import WireLimits, _exchange

STORE_URL: Final = "http://serpro-broker:8080/procurador"


def store_procurador(firm: str, token: TermoToken) -> None:
    """Only the internal endpoint receives values; callers must not persist them."""
    assert_no_transaction()
    body = json.dumps(
        {
            "firm_cnpj": firm,
            "procurador": token.procurador_token,
            "expires_at": token.expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    secret = os.environ.get("SERPRO_BROKER_SECRET", "")
    if not secret:
        code = "broker_configuracao_invalida"
        raise TransportFailure(code)
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = _exchange(
        Request(
            "POST",
            STORE_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Broker-Token-HMAC": signature,
            },
        ),
        WireLimits(size=65536),
    )
    if isinstance(response, str) or response.status != 200:  # noqa: PLR2004
        code = "broker_recusado"
        raise TransportFailure(code)
