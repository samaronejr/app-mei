"""AXRM-020: v2 adds fiscal identity without changing portal extractor v1."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.cycles.extraction import EXTRACTOR_VERSION
from apps.cycles.extraction.v2 import EXTRACTOR_VERSION as V2
from apps.cycles.extraction.v2 import KNOWN_VERSIONS, extract
from apps.cycles.parsing import UnsafeDocument
from apps.obligations.fiscal_ingest import FiscalSource, _payload
from tests.obligations.fiscal_fixtures import (
    ALPHANUMERIC,
    CLIENT,
    COUNTERPART,
    KEY,
    nfe,
    nfse,
    summary,
)


def source_context() -> FiscalSource:
    return FiscalSource(
        tenant_id=UUID("00000000-0000-7000-8000-000000000001"),
        client_id=UUID("00000000-0000-7000-8000-000000000002"),
        source="captura",
        provider="focus",
        provider_version="12",
        environment="fake",
        client_role="destinatario",
        source_namespace="focus:nfe_recebidas",
        receipt_ref="fake-call-12",
        actor=None,
        capture_binding_id=None,
    )


def clean() -> DocumentIntake:
    return DocumentIntake(state=IntakeState.VERIFIED)


def test_full_nfe_identity_parties_dates_amounts_and_provenance() -> None:
    result = extract(clean(), nfe(issuer=ALPHANUMERIC))
    values = {field.name: field.value for field in result.fields}
    assert (V2, EXTRACTOR_VERSION) == ("2", "r1.1")
    assert (result.family, result.key_namespace, result.document_key) == (
        "nfe",
        "sefaz",
        KEY,
    )
    assert (result.summary_only, result.fiscal_state, result.note) == (
        False,
        "autorizada",
        "",
    )
    assert values["emitente_id"] == ALPHANUMERIC
    assert values["destinatario_id"] == CLIENT
    assert values["emitente_name"] == "Synthetic issuer"
    assert values["destinatario_name"] == "Synthetic recipient"
    assert values["issue_date"] == "2026-09-15"
    assert values["total"] == "1500.25"
    assert all(field.origin == "xml" for field in result.fields)
    assert next(f for f in result.fields if f.name == "total").source_span == (
        "total/ICMSTot/vNF"
    )


@pytest.mark.parametrize("version", ["1.00", "1.01"])
def test_current_national_versions_extract_iss_and_tolerate_unknown_tax_groups(
    version: str,
) -> None:
    extra = "<IBSCBS><futureUnknown><value>untrusted</value></futureUnknown></IBSCBS>"
    result = extract(clean(), nfse(version=version, extra=extra))
    values = {field.name: field.value for field in result.fields}
    assert version in KNOWN_VERSIONS
    assert result.family == "nfse_nacional"
    assert result.key_namespace == "nacional"
    assert result.fiscal_state == "autorizada"
    assert result.document_key == f"NFS{KEY}"
    assert result.note == ""
    assert values["prestador_id"] == ALPHANUMERIC
    assert values["tomador_id"] == CLIENT
    assert values["prestador_name"] == "Synthetic prestador"
    assert values["tomador_name"] == "Synthetic tomador"
    assert values["issue_date"] == "2026-09-15"
    assert values["competence"] == "2026-09-01"
    assert values["municipality_incidence"] == "3303302"
    assert values["total"] == "800.00"
    assert values["iss_stated"] == "12.34"
    assert values["iss_withheld_flag"] == "2"


def test_unknown_national_version_requires_review_and_does_not_guess_fields() -> None:
    result = extract(clean(), nfse(version="99.99"))
    assert (
        result.family,
        result.key_namespace,
        result.document_key,
        result.fiscal_state,
    ) == ("nfse_nacional", "nacional", f"NFS{KEY}", "desconhecida")
    assert result.note == "layout nao reconhecido"
    assert result.fields == ()


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>',
        b'<!DOCTYPE a [<!ENTITY x "ha"><!ENTITY y "&x;&x;&x;">]><a>&y;</a>',
        b"<broken",
    ],
)
def test_unsafe_or_malformed_xml_is_refused(payload: bytes) -> None:
    with pytest.raises(UnsafeDocument):
        extract(clean(), payload)


def test_nothing_is_parsed_before_scanner_verifies() -> None:
    with pytest.raises(UnsafeDocument, match="not_verified"):
        extract(DocumentIntake(state=IntakeState.QUARANTINED), nfe())


def test_exact_five_mib_boundary_is_accepted_and_one_byte_more_refused() -> None:
    raw = nfe()
    payload = raw + b" " * (5 * 1024 * 1024 - len(raw))
    assert extract(clean(), payload).document_key == KEY
    with pytest.raises(UnsafeDocument, match="too_large"):
        extract(clean(), payload + b" ")


def test_nfe_cpf_and_cancellation_status_are_supported() -> None:
    payload = nfe(state="101").replace(
        f"<CNPJ>{CLIENT}</CNPJ>".encode(), b"<CPF>00000000191</CPF>"
    )
    result = extract(clean(), payload)
    assert result.fiscal_state == "cancelada"
    assert {f.name: f.value for f in result.fields}["destinatario_id"] == "00000000191"
    assert {f.name: f.value for f in result.fields}["emitente_id"] == COUNTERPART


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("100", "autorizada"),
        ("150", "autorizada"),
        ("101", "cancelada"),
        ("151", "cancelada"),
        ("110", "denegada"),
        ("301", "denegada"),
        ("302", "denegada"),
        ("999", "desconhecida"),
    ],
)
def test_nfe_fiscal_status_codes_have_honest_unknown_fallback(
    status: str, expected: str
) -> None:
    assert extract(clean(), nfe(state=status)).fiscal_state == expected


def test_xml_field_values_are_bounded_without_silent_truncation() -> None:
    raw = nfe().replace(b"Synthetic issuer", b"x" * 256)
    with pytest.raises(UnsafeDocument, match="field_too_long"):
        extract(clean(), raw)


def test_missing_identity_is_refused_by_the_full_xml_extractor() -> None:
    raw = nfe().replace(f'Id="NFe{KEY}"'.encode(), b'Id="OtherKey"')
    with pytest.raises(UnsafeDocument, match="missing_fiscal_key"):
        extract(clean(), raw)


def test_national_prestador_fallback_and_blank_fields() -> None:
    payload = nfse().replace(
        (
            f"<emit><CNPJ>{ALPHANUMERIC}</CNPJ>"
            "<xNome>Synthetic prestador</xNome></emit>"
        ).encode(),
        b"<emit><xNome> </xNome></emit>",
    )
    result = extract(clean(), payload)
    assert {field.name: field.value for field in result.fields}[
        "prestador_id"
    ] == ALPHANUMERIC
    assert "prestador_name" not in {field.name for field in result.fields}


def test_unrelated_xml_is_refused() -> None:
    with pytest.raises(UnsafeDocument, match="unsupported_fiscal_xml"):
        extract(clean(), b"<unrelated/>")


@pytest.mark.parametrize("error", [LookupError, KeyboardInterrupt])
def test_projection_does_not_mask_programming_failures_or_interrupts(
    error: type[BaseException],
) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from apps.cycles.extraction import FieldValue  # noqa: PLC0415
    from apps.cycles.extraction.v2 import FiscalExtraction  # noqa: PLC0415
    from apps.obligations.fiscal_ingest import _projection  # noqa: PLC0415

    extraction = FiscalExtraction(
        "nfe",
        "sefaz",
        KEY,
        "autorizada",
        fields=(FieldValue("total", "1.00", "xml", "total/ICMSTot/vNF"),),
    )
    with (
        patch("apps.obligations.fiscal_ingest._money", side_effect=error),
        pytest.raises(error),
    ):
        _projection(extraction)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "unknown"),
        ("environment", "unknown"),
        ("client_role", "unknown"),
        ("provider_version", ""),
        ("provider_version", "not-a-version"),
        ("provider_version", "\uff11\uff12"),
        ("provider_version", "1" * 65),
        ("source_namespace", ""),
        ("source_namespace", "x" * 65),
        ("receipt_ref", ""),
    ],
)
def test_invalid_ingest_context_is_refused_before_io(field: str, value: str) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    original = source_context()
    context = replace(
        original,
        source=value if field == "source" else original.source,
        environment=value if field == "environment" else original.environment,
        client_role=value if field == "client_role" else original.client_role,
        provider_version=value
        if field == "provider_version"
        else original.provider_version,
        source_namespace=value
        if field == "source_namespace"
        else original.source_namespace,
        receipt_ref=value if field == "receipt_ref" else original.receipt_ref,
    )
    with pytest.raises(ValidationError, match="Contexto fiscal invalido"):
        _payload(context, nfe(), None)


@pytest.mark.parametrize(
    ("payload", "summary_value"),
    [(b"<xml/>", {"chNFe": KEY}), (None, None)],
)
def test_ingest_requires_exactly_one_payload(
    payload: bytes | None, summary_value: dict[str, str] | None
) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    context = replace(
        source_context(),
        capture_binding_id=UUID("00000000-0000-7000-8000-000000000003"),
    )
    with pytest.raises(ValidationError, match="Contexto fiscal invalido"):
        _payload(context, payload, summary_value)


def test_capture_cannot_invent_a_human_actor() -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    from apps.accounts.models import User  # noqa: PLC0415

    context = replace(source_context(), actor=User(email="synthetic@example.test"))
    with pytest.raises(ValidationError):
        _payload(context, nfe(), None)


@pytest.mark.parametrize("source", ["importacao_equipe", "upload_cliente"])
def test_human_sources_require_their_real_actor(source: str) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError):
        _payload(replace(source_context(), source=source), nfe(), None)


@pytest.mark.parametrize(
    "total", ["1000000000000", "-0.01", "0.001", "NaN", "Infinity", "bad"]
)
def test_projection_refuses_out_of_range_or_non_cent_amounts(total: str) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    from apps.cycles.extraction import FieldValue  # noqa: PLC0415
    from apps.cycles.extraction.v2 import FiscalExtraction  # noqa: PLC0415
    from apps.obligations.fiscal_ingest import _projection  # noqa: PLC0415

    extraction = FiscalExtraction(
        "nfe",
        "sefaz",
        KEY,
        "autorizada",
        fields=(FieldValue("total", total, "xml"),),
    )
    with pytest.raises(ValidationError, match="Valor ou data fiscal invalida"):
        _projection(extraction)


@pytest.mark.parametrize("total", ["0", "0.01", "999999999999.99"])
def test_projection_accepts_the_declared_decimal_boundaries(total: str) -> None:
    from decimal import Decimal  # noqa: PLC0415

    from apps.cycles.extraction import FieldValue  # noqa: PLC0415
    from apps.cycles.extraction.v2 import FiscalExtraction  # noqa: PLC0415
    from apps.obligations.fiscal_ingest import _projection  # noqa: PLC0415

    extraction = FiscalExtraction(
        "nfe",
        "sefaz",
        KEY,
        "autorizada",
        fields=(FieldValue("total", total, "xml"),),
    )
    assert _projection(extraction)["total"] == Decimal(total)


def test_ingest_reference_length_accepts_exactly_sixty_four_characters() -> None:
    context = replace(
        source_context(), provider_version="1" * 64, source_namespace="x" * 64
    )
    assert _payload(context, nfe(), None) == nfe()


@pytest.mark.parametrize(
    ("fiscal_state", "replacement_key", "source_effective_at"),
    [
        ("incerta", "", None),
        ("not-a-state", "", None),
        ("substituida", "x" * 129, None),
        (None, "NFS123", None),
        (None, "", datetime.fromisoformat("2026-09-16T14:30:00")),
    ],
)
def test_invalid_provider_lifecycle_metadata_is_refused(
    fiscal_state: str | None,
    replacement_key: str,
    source_effective_at: datetime | None,
) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    context = replace(
        source_context(),
        fiscal_state=fiscal_state,
        replacement_key=replacement_key,
        source_effective_at=source_effective_at,
    )
    with pytest.raises(ValidationError, match="Contexto fiscal invalido"):
        _payload(context, nfe(), None)


def test_provider_lifecycle_metadata_accepts_aware_time_and_replacement_boundary() -> (
    None
):
    context = replace(
        source_context(),
        fiscal_state="substituida",
        replacement_key="x" * 128,
        source_effective_at=datetime(2026, 9, 16, 14, 30, tzinfo=UTC),
    )
    assert _payload(context, nfe(), None) == nfe()


def test_xml_value_accepts_exactly_two_hundred_fifty_five_characters() -> None:
    raw = nfe().replace(b"Synthetic issuer", b"x" * 255)
    values = {field.name: field.value for field in extract(clean(), raw).fields}
    assert values["emitente_name"] == "x" * 255


@pytest.mark.parametrize(
    ("indicator", "expected"),
    [("1", False), ("2", True), ("3", True), ("9", None), ("", None)],
)
def test_retention_projection_preserves_unknown_indicator(
    indicator: str, expected: bool | None
) -> None:
    from decimal import Decimal  # noqa: PLC0415

    from apps.cycles.extraction import FieldValue  # noqa: PLC0415
    from apps.cycles.extraction.v2 import FiscalExtraction  # noqa: PLC0415
    from apps.obligations.fiscal_ingest import _projection  # noqa: PLC0415

    extraction = FiscalExtraction(
        "nfse_nacional",
        "nacional",
        f"NFS{KEY}",
        "autorizada",
        fields=(
            FieldValue("iss_stated", "12.34", "xml"),
            FieldValue("iss_withheld_flag", indicator, "xml"),
        ),
    )
    projection = _projection(extraction)
    assert projection["iss_withheld_flag"] is expected
    assert projection["iss_withheld_amount"] == (Decimal("12.34") if expected else None)


def test_overlong_party_identity_is_refused_before_typed_register_storage() -> None:
    with pytest.raises(UnsafeDocument, match="invalid_party_id"):
        extract(clean(), nfe(issuer=COUNTERPART + "0"))


@pytest.mark.parametrize(
    "municipality",
    ["33033020", "ABC3302", "\uff13\uff13\uff10\uff13\uff13\uff10\uff12"],
)
def test_malformed_incidence_municipality_is_refused(municipality: str) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    from apps.cycles.extraction import FieldValue  # noqa: PLC0415
    from apps.cycles.extraction.v2 import FiscalExtraction  # noqa: PLC0415
    from apps.obligations.fiscal_ingest import _projection  # noqa: PLC0415

    extraction = FiscalExtraction(
        "nfse_nacional",
        "nacional",
        f"NFS{KEY}",
        "autorizada",
        fields=(FieldValue("municipality_incidence", municipality, "xml"),),
    )
    with pytest.raises(ValidationError, match="Municipio de incidencia invalido"):
        _projection(extraction)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("1", "autorizada"),
        ("2", "denegada"),
        ("3", "cancelada"),
        ("999", "desconhecida"),
    ],
)
def test_bound_summary_serialization_and_extraction_cover_all_fields(
    status: str, expected: str
) -> None:
    context = replace(
        source_context(),
        capture_binding_id=UUID("00000000-0000-7000-8000-000000000003"),
    )
    content = summary(state=status)
    content["xNome"] = "Synthetic & issuer"
    content["unrecognized"] = "not part of the fiscal summary"
    payload = _payload(context, None, content)
    assert payload.startswith(b"<?xml")
    assert b"unrecognized" not in payload
    assert _payload(context, None, dict(reversed(list(content.items())))) == payload
    result = extract(clean(), payload)
    assert (
        result.family,
        result.key_namespace,
        result.document_key,
        result.fiscal_state,
        result.summary_only,
        result.note,
    ) == ("nfe", "sefaz", KEY, expected, True, "")
    assert {field.name: field.value for field in result.fields} == {
        "emitente_id": COUNTERPART,
        "emitente_name": "Synthetic & issuer",
        "issue_date": "2026-09-15",
        "total": "1500.25",
    }
    assert all(field.origin == "xml" for field in result.fields)


def test_summary_cpf_issuer_is_preserved() -> None:
    context = replace(
        source_context(),
        capture_binding_id=UUID("00000000-0000-7000-8000-000000000003"),
    )
    content = summary()
    del content["CNPJ"]
    content["CPF"] = "00000000191"
    result = extract(clean(), _payload(context, None, content))
    assert {field.name: field.value for field in result.fields}[
        "emitente_id"
    ] == "00000000191"


@pytest.mark.parametrize("source", ["captura", "importacao_equipe", "upload_cliente"])
def test_summary_requires_capture_and_an_explicit_binding(source: str) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    from apps.accounts.models import User  # noqa: PLC0415

    context = replace(
        source_context(),
        source=source,
        actor=None if source == "captura" else User(email="synthetic@example.test"),
        capture_binding_id=(
            None
            if source == "captura"
            else UUID("00000000-0000-7000-8000-000000000003")
        ),
    )
    with pytest.raises(ValidationError, match="vinculo de captura"):
        _payload(context, None, summary())


@pytest.mark.parametrize("source", ["importacao_equipe", "upload_cliente"])
def test_human_originals_accept_real_actor_without_provider_metadata(
    source: str,
) -> None:
    from apps.accounts.models import User  # noqa: PLC0415

    context = replace(
        source_context(),
        source=source,
        actor=User(email="synthetic@example.test"),
        receipt_ref="",
    )
    assert _payload(context, nfe(), None) == nfe()


def test_unknown_source_is_refused_even_with_a_real_actor() -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    from apps.accounts.models import User  # noqa: PLC0415

    context = replace(
        source_context(), source="unknown", actor=User(email="synthetic@example.test")
    )
    with pytest.raises(ValidationError, match="Contexto fiscal invalido"):
        _payload(context, nfe(), None)


@pytest.mark.parametrize(
    "state", ["autorizada", "cancelada", "substituida", "denegada", "desconhecida"]
)
def test_capture_accepts_each_documented_provider_state(state: str) -> None:
    assert _payload(replace(source_context(), fiscal_state=state), nfe(), None) == nfe()


@pytest.mark.parametrize(
    ("state", "effective"),
    [("cancelada", None), (None, datetime(2026, 9, 16, 14, 30, tzinfo=UTC))],
)
def test_human_source_cannot_assert_provider_state_or_effective_time(
    state: str | None, effective: datetime | None
) -> None:
    from django.core.exceptions import ValidationError  # noqa: PLC0415

    from apps.accounts.models import User  # noqa: PLC0415

    context = replace(
        source_context(),
        source="importacao_equipe",
        actor=User(email="synthetic@example.test"),
        fiscal_state=state,
        source_effective_at=effective,
    )
    with pytest.raises(ValidationError, match="Contexto fiscal invalido"):
        _payload(context, nfe(), None)


def test_payload_cap_has_the_same_exact_boundary_before_vault_io() -> None:
    raw = nfe()
    payload = raw + b" " * (5 * 1024 * 1024 - len(raw))
    assert _payload(source_context(), payload, None) == payload
    with pytest.raises(UnsafeDocument, match="too_large"):
        _payload(source_context(), payload + b" ", None)


def test_fiscal_routing_and_extraction_values_are_immutable() -> None:
    for value, field in (
        (source_context(), "provider"),
        (extract(clean(), nfe()), "document_key"),
    ):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, "mutated")


def test_projection_preserves_every_typed_national_field() -> None:
    from decimal import Decimal  # noqa: PLC0415

    from apps.obligations.fiscal_ingest import _projection  # noqa: PLC0415

    assert _projection(extract(clean(), nfse())) == {
        "issue_date": date(2026, 9, 15),
        "competence": date(2026, 9, 1),
        "total": Decimal("800.00"),
        "iss_stated": Decimal("12.34"),
        "municipality_incidence": "3303302",
        "iss_withheld_flag": True,
        "iss_withheld_amount": Decimal("12.34"),
    }


@pytest.mark.parametrize(
    "identifier",
    ["11.444.777/0001-61", "12abc34501de35"],
)
def test_party_masks_and_case_use_the_shared_normalization(identifier: str) -> None:
    result = extract(clean(), nfe(issuer=identifier))
    expected = COUNTERPART if "." in identifier else ALPHANUMERIC
    assert {field.name: field.value for field in result.fields}[
        "emitente_id"
    ] == expected


@pytest.mark.parametrize("identifier", ["A" * 11, "\uff11" * 11, "00000000!91"])
def test_cpf_identifiers_require_ascii_digits(identifier: str) -> None:
    payload = nfe().replace(
        f"<CNPJ>{COUNTERPART}</CNPJ>".encode(), f"<CPF>{identifier}</CPF>".encode()
    )
    with pytest.raises(UnsafeDocument, match="invalid_party_id"):
        extract(clean(), payload)


@pytest.mark.parametrize("identifier", ["\uff11" * 14, "11444777!00161"])
def test_cnpj_identifiers_require_ascii_alphanumeric_characters(
    identifier: str,
) -> None:
    with pytest.raises(UnsafeDocument, match="invalid_party_id"):
        extract(clean(), nfe(issuer=identifier))


def test_issuer_cpf_and_legacy_issue_date_path_are_supported() -> None:
    payload = (
        nfe()
        .replace(f"<CNPJ>{COUNTERPART}</CNPJ>".encode(), b"<CPF>00000000191</CPF>")
        .replace(
            b"<dhEmi>2026-09-15T12:30:00-03:00</dhEmi>",
            b"<dEmi>2026-09-14</dEmi>",
        )
    )
    values = {field.name: field.value for field in extract(clean(), payload).fields}
    assert values["emitente_id"] == "00000000191"
    assert values["issue_date"] == "2026-09-14"


@pytest.mark.parametrize("path", ["emit", "fallback"])
def test_national_cpf_alternatives_preserve_both_parties(path: str) -> None:
    payload = nfse().replace(
        f"<toma><CNPJ>{CLIENT}</CNPJ>".encode(), b"<toma><CPF>00000000272</CPF>"
    )
    if path == "emit":
        payload = payload.replace(
            f"<emit><CNPJ>{ALPHANUMERIC}</CNPJ>".encode(),
            b"<emit><CPF>00000000191</CPF>",
        )
    else:
        payload = payload.replace(
            (
                f"<emit><CNPJ>{ALPHANUMERIC}</CNPJ>"
                "<xNome>Synthetic prestador</xNome></emit>"
            ).encode(),
            b"",
        ).replace(
            f"<prest><CNPJ>{ALPHANUMERIC}</CNPJ></prest>".encode(),
            b"<prest><CPF>00000000191</CPF></prest>",
        )
    values = {field.name: field.value for field in extract(clean(), payload).fields}
    assert values["prestador_id"] == "00000000191"
    assert values["tomador_id"] == "00000000272"


def test_national_issuer_path_precedes_the_dps_fallback() -> None:
    payload = nfse().replace(
        f"<emit><CNPJ>{ALPHANUMERIC}</CNPJ>".encode(),
        f"<emit><CNPJ>{COUNTERPART}</CNPJ>".encode(),
    )
    field = next(
        field
        for field in extract(clean(), payload).fields
        if field.name == "prestador_id"
    )
    assert (field.value, field.source_span) == (COUNTERPART, "emit/CNPJ")


@pytest.mark.parametrize(
    "payload",
    [
        b'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"/>',
        b"<foreign>" + nfe() + b"</foreign>",
        b'<NFSe xmlns="http://www.sped.fazenda.gov.br/nfse" versao="1.01"/>',
        nfse().replace(b"<NFSe ", b"<Other ").replace(b"</NFSe>", b"</Other>"),
    ],
)
def test_namespace_and_required_info_nodes_are_checked(payload: bytes) -> None:
    with pytest.raises(UnsafeDocument, match="unsupported_fiscal_xml"):
        extract(clean(), payload)


def test_bare_nfe_info_is_read_without_fabricating_protocol_status() -> None:
    raw = nfe()
    info = raw[raw.index(b"<infNFe") : raw.index(b"</infNFe>") + len(b"</infNFe>")]
    payload = info.replace(
        b"<infNFe ", b'<infNFe xmlns="http://www.portalfiscal.inf.br/nfe" ', 1
    )
    result = extract(clean(), payload)
    assert (result.document_key, result.fiscal_state) == (KEY, "desconhecida")


def test_missing_identity_attributes_stay_missing() -> None:
    with pytest.raises(UnsafeDocument, match="missing_fiscal_key"):
        extract(clean(), nfe().replace(f'Id="NFe{KEY}"'.encode(), b""))
    assert (
        extract(clean(), nfse().replace(f'Id="NFS{KEY}"'.encode(), b"")).document_key
        == ""
    )


@pytest.mark.parametrize(
    "raw", ["NaN", "Infinity", "-Infinity", "-0.01", "0.001", "1000000000000"]
)
def test_money_boundary_refusals_preserve_the_rejected_scalar(raw: str) -> None:
    from apps.obligations.fiscal_ingest import _money  # noqa: PLC0415

    with pytest.raises(ValueError, match=raw) as error:
        _money(raw)
    assert error.value.args == (raw,)
