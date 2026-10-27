"""Derive a client call's SERPRO parties from the firm's accepted signed termo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.integrations.serpro.codec import CnpjInvalidoError, Parties
from apps.integrations.serpro.termo import (
    MAX_XML,
    TermoInvalidoError,
    read_signed_termo,
)
from apps.integrations.serpro.termo_state import termo_head, termo_in_force

if TYPE_CHECKING:
    from uuid import UUID


def termo_parties(connection_id: UUID, client_cnpj: str) -> Parties | None:
    """Return (contratante, firm, client) from the termo in force, else None.

    The contratante and the firm are exactly the `destinatario` and `assinadoPor`
    of the signed termo SERPRO accepted for this connection; Axorim stores no other
    copy of either identifier, so nothing here can name a different firm.
    """
    if not termo_in_force(connection_id, timezone.localdate()):
        return None
    head = termo_head(connection_id)
    if head is None:
        return None
    document = head.signed_document
    with default_storage.open(document.storage_key, "rb") as handle:
        raw = handle.read(MAX_XML + 1)
    try:
        data = ElementTree.fromstring(
            raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
        ).find("dados")
        if data is None:
            return None
        recipient, author = data.find("destinatario"), data.find("assinadoPor")
        if recipient is None or author is None:
            return None
        contratante, firm = recipient.get("numero", ""), author.get("numero", "")
        read_signed_termo(raw, Parties(contratante, firm, firm))
        return Parties(contratante, firm, client_cnpj)
    except (
        ElementTree.ParseError,
        DefusedXmlException,
        TermoInvalidoError,
        CnpjInvalidoError,
    ):
        return None
