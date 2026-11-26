"""AXRM-021 pages are indivisible, fenced and restart from their committed cursor."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink
from apps.cycles.records import Exception as CycleException
from apps.cycles.scanning import FakeScanner
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.obligations.models import FiscalRevision, RevisionArtifact
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.capture_fixtures import (
    MONTH,
    START,
    CaptureWorld,
    capture_api,
    capture_world,
    fiscal_rows,
    item,
    key,
    model,
    national_item,
    page,
    xml,
)
from tests.obligations.fiscal_fixtures import nfse

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from apps.cycles.evidence import DocumentIntake
    from apps.cycles.extraction.v2 import FiscalExtraction
    from apps.integrations.focus.capture import PreparedPage
    from apps.integrations.transport_types import ProviderRequest, ProviderResponse
    from apps.obligations.fiscal_ingest import FiscalSource
    from apps.obligations.iss_retention_evidence_v1 import ISSDecision, ISSOriginal
    from apps.obligations.models import Document, FiscalDocument

pytestmark = pytest.mark.django_db(transaction=True)


class Interrupted(BaseException):
    """A deterministic process death, deliberately outside Exception handlers."""


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CaptureWorld]:
    graph = capture_world(settings, tmp_path, monkeypatch)
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        yield graph


def test_stream_identity_contains_binding_collection_filter_digest_and_epoch(
    world: CaptureWorld,
) -> None:
    api = capture_api()
    stream = world.stream()
    assert stream.pk == world.stream().pk
    assert stream.pk != world.stream("nfsen_recebidas").pk
    assert stream.pk != world.stream(epoch=2).pk
    assert (stream.committed_cursor, stream.cursor_version) == ("0", 1)
    with tenant_context(world.tenant_id):
        for forbidden in ("pendente", "pendente_ciencia"):
            with pytest.raises(ValidationError):
                api.ensure_stream(world.link, "nfe_recebidas", filters={forbidden: "1"})
    assert len(stream.filter_digest) == 64


def test_stream_has_four_independent_timestamps_and_portal_private_runs(
    world: CaptureWorld,
) -> None:
    stream_type, run_type = model("SyncStream"), model("SyncRun")
    fields = {field.name for field in stream_type._meta.fields}
    assert {
        "latest_attempt_at",
        "last_api_success_at",
        "last_successful_source_observation_at",
        "last_complete_scan_at",
        "last_error_code",
        "consecutive_failures",
    } <= fields
    assert {"client", "tenant", "stream", "occurrence_key", "fence", "lease_until"} <= {
        field.name for field in run_type._meta.fields
    }
    assert any(
        getattr(constraint, "condition", None) is not None
        and "stream" in getattr(constraint, "fields", ())
        for constraint in run_type._meta.constraints
    )


def test_cursor_uses_maximum_returned_version_and_retains_the_raw_page_first(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream()
    rows = [item(481, index=1), item(484, index=2)]
    with FakeFocus((page(rows, maximum=999999, total=9000),)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run is not None
    stream = world.reload(stream)
    assert stream.committed_cursor == "484"
    assert stream.cursor_version == 2
    assert stream.latest_attempt_at == START
    assert stream.last_api_success_at == START
    assert stream.last_successful_source_observation_at == START
    assert stream.last_complete_scan_at is None
    assert run.state == "parcial", "Summary bytes do not complete XML coverage."
    assert len(fiscal_rows(world)) == 2
    with tenant_context(world.tenant_id):
        revisions = list(FiscalRevision.objects.order_by("provider_version"))
        assert not EvidenceLink.objects.filter(
            document__acquisition_origin="captura_provedor"
        ).exists()
    assert {revision.provider_version for revision in revisions} == {"481", "484"}
    for revision in revisions:
        assert revision.receipt_ref.startswith("capture-pages/")
        with default_storage.open(revision.receipt_ref, "rb") as stored:
            assert json.loads(stored.read()) == rows
    query = parse_qs(urlsplit(fake.requests[0].path).query)
    assert query == {"cnpj": [world.link.client.cnpj], "versao": ["0"]}


def test_crash_after_fetch_replays_from_the_committed_cursor_without_duplicates(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    rows = [item(1), item(4, index=2)]
    commit = api._commit_page
    with FakeFocus((page(rows), page(rows))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        with (
            patch.object(api, "_commit_page", side_effect=Interrupted),
            pytest.raises(Interrupted),
        ):
            api.synchronize(world.tenant_id, stream.pk)
        assert world.reload(stream).committed_cursor == "0"
        assert fiscal_rows(world) == []
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        with patch.object(api, "_commit_page", wraps=commit):
            api.synchronize(world.tenant_id, stream.pk)
    assert world.reload(stream).committed_cursor == "4"
    assert len(fiscal_rows(world)) == 2
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 2
    assert [
        parse_qs(urlsplit(request.path).query)["versao"] for request in fake.requests
    ] == [["0"], ["0"]]


def test_crash_during_the_second_registration_rolls_back_the_entire_page(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.obligations import fiscal_ingest  # noqa: PLC0415

    api, stream = capture_api(), world.stream()
    register = fiscal_ingest._register
    registered = 0

    def interrupted(
        context: FiscalSource,
        original: Document,
        intake: DocumentIntake,
        extraction: FiscalExtraction,
        iss_originals: Mapping[ISSOriginal, ISSDecision],
    ) -> FiscalDocument:
        nonlocal registered
        assert connection.in_atomic_block
        registered += 1
        if registered == 2:
            raise Interrupted
        return register(context, original, intake, extraction, iss_originals)

    rows = [item(1), item(2, index=2)]
    with FakeFocus((page(rows), page(rows))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        with (
            patch.object(fiscal_ingest, "_register", side_effect=interrupted),
            pytest.raises(Interrupted),
        ):
            api.synchronize(world.tenant_id, stream.pk)
        assert fiscal_rows(world) == []
        assert world.reload(stream).committed_cursor == "0"
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
    assert len(fiscal_rows(world)) == 2
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 2


@pytest.mark.parametrize("seconds", [1, 3599])
def test_a_stream_is_never_polled_faster_than_hourly(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    seconds: int,
) -> None:
    api, stream = capture_api(), world.stream()
    with FakeFocus((page([]), page([]))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        assert api.synchronize(world.tenant_id, stream.pk) is not None
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(seconds=seconds))
        assert api.synchronize(world.tenant_id, stream.pk) is None
        assert len(fake.requests) == 1
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        assert api.synchronize(world.tenant_id, stream.pk) is not None
        assert len(fake.requests) == 2


def test_exactly_three_full_pages_end_partial_and_next_run_continues(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    batches = [
        [item(index, index=index) for index in range(start, start + 100)]
        for start in (1, 101, 201)
    ]
    with FakeFocus(
        (*[page(rows) for rows in batches], page([item(304, index=304)]))
    ) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
        assert (run.state, run.pages_committed) == ("parcial", 3)
        assert world.reload(stream).committed_cursor == "300"
        assert len(fake.requests) == 3
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
    assert world.reload(stream).committed_cursor == "304"
    assert len(fiscal_rows(world)) == 301
    assert [
        parse_qs(urlsplit(request.path).query)["versao"][0] for request in fake.requests
    ] == ["0", "100", "200", "300"]


def test_full_xml_is_fetched_outside_transactions_and_a_cancellation_appends(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.integrations.transport import ProviderTransport  # noqa: PLC0415

    api, stream = capture_api(), world.stream()
    send = ProviderTransport.send

    def outside(
        transport: ProviderTransport, operation: str, request: ProviderRequest
    ) -> ProviderResponse:
        assert not connection.in_atomic_block
        return send(transport, operation, request)

    replies = (
        page([item(1, full=True)]),
        Reply(body=xml(), headers=(("Content-Type", "application/xml"),)),
        page([item(5, full=True, state="cancelada")]),
        Reply(body=xml(), headers=(("Content-Type", "application/xml"),)),
    )
    with FakeFocus(replies) as fake, patch.object(ProviderTransport, "send", outside):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        first = api.synchronize(world.tenant_id, stream.pk)
        assert first.state == "concluida"
        assert world.reload(stream).last_complete_scan_at == START
        previous = fiscal_rows(world)[0].current_revision_id
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
    current = fiscal_rows(world)[0]
    assert current.fiscal_state == "cancelada"
    assert current.current_revision_id != previous
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 2
    assert fake.requests[1].path == f"/v2/nfes_recebidas/{key(1)}.xml"


def test_nfse_requests_completa_and_retains_the_xml_for_its_competence(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream("nfsen_recebidas")
    row = {
        "chave_nfse": key(1),
        "versao": 8,
        "situacao": "autorizado",
        "data_emissao": "2026-09-15T12:30:00-03:00",
    }
    payload = nfse().replace(("33" + "0" * 42).encode(), key(1).encode())
    with FakeFocus(
        (
            page([row]),
            Reply(body=payload, headers=(("Content-Type", "application/xml"),)),
        )
    ) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, stream.pk)
    fiscal = fiscal_rows(world)[0]
    assert (fiscal.family, fiscal.competence, fiscal.summary_only) == (
        "nfse_nacional",
        MONTH,
        False,
    )
    query = parse_qs(urlsplit(fake.requests[0].path).query)
    assert query == {
        "cnpj": [world.link.client.cnpj],
        "versao": ["0"],
        "completa": ["1"],
    }
    assert fake.requests[1].path == f"/v2/nfsens_recebidas/{key(1)}.xml"


def test_nfse_capture_refreshes_changed_originals_before_committing_a_page(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream("nfsen_recebidas")
    row, payload = national_item()
    padded = payload.replace(b">2</tpRetISSQN>", b"> 2 </tpRetISSQN>")
    prepare = api._prepare_page
    verifier = api.fiscal_ingest.verify_iss_original

    def attach_between_snapshot_and_commit(
        *args: object, **kwargs: object
    ) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        competing = api.fiscal_ingest.prepare_fiscal_document(
            prepared.entries[0].fiscal.context, payload_bytes=padded, summary=None
        )
        assert competing is not None
        with tenant_context(world.tenant_id):
            competing.register(prepared.receipt_ref)
        return prepared

    def verify_outside_transaction(witness: ISSOriginal) -> ISSDecision:
        assert not connection.in_atomic_block
        return cast("ISSDecision", verifier(witness))

    with (
        FakeFocus((page([row]), Reply(body=payload))) as fake,
        patch.object(
            api, "_prepare_page", side_effect=attach_between_snapshot_and_commit
        ),
        patch.object(
            api.fiscal_ingest,
            "verify_iss_original",
            side_effect=verify_outside_transaction,
        ),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run.state == "concluida"
    assert len(fake.requests) == 2  # No provider replay on the DB snapshot retry.
    assert world.reload(stream).committed_cursor == "8"
    current = fiscal_rows(world)[0]
    assert (
        current.iss_retention_party,
        current.iss_withheld_flag,
        current.iss_withheld_amount,
    ) == ("desconhecido", None, None)
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 1
        assert (
            RevisionArtifact.objects.filter(
                revision_id=current.current_revision_id, role="xml_original"
            ).count()
            == 2
        )


def test_nfse_one_page_can_attach_two_prepared_originals_to_the_same_revision(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream("nfsen_recebidas")
    row, payload = national_item()
    other_row, other_payload = national_item(2, 9)
    other_payload = other_payload.replace(b">2</tpRetISSQN>", b">3</tpRetISSQN>")
    prepare = api._prepare_page

    def prepare_two_originals(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        first, other, second = prepared.entries
        # Simulate two byte-distinct variants prepared in the same page;
        # Focus's request cache would otherwise return the first XML twice.
        distinct = api.fiscal_ingest.prepare_fiscal_document(
            second.fiscal.context,
            payload_bytes=payload.replace(b">2</tpRetISSQN>", b"> 2 </tpRetISSQN>"),
            summary=None,
        )
        assert distinct is not None
        third = replace(second, fiscal=distinct)
        return replace(prepared, entries=(first, other, third))

    replies = (
        page([row, other_row, row]),
        Reply(body=payload),
        Reply(body=other_payload),
    )
    with (
        FakeFocus(replies) as fake,
        patch.object(api, "_prepare_page", side_effect=prepare_two_originals),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run.state == "concluida"
    assert world.reload(stream).committed_cursor == "9"
    docs = {doc.document_key: doc for doc in fiscal_rows(world)}
    first, other = docs[f"NFS{key(1)}"], docs[f"NFS{key(2)}"]
    assert first.iss_retention_party == "desconhecido"
    assert other.iss_retention_party == "intermediario"
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 2
        ids = list(RevisionArtifact.objects.values_list("revision_id", flat=True))
        assert first.current_revision_id is not None
        assert other.current_revision_id is not None
        assert ids.count(first.current_revision_id) == 2
        assert ids.count(other.current_revision_id) == 1


def test_nfse_second_stale_snapshot_refuses_the_page_without_advancing_cursor(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream("nfsen_recebidas")
    row, payload = national_item()
    with (
        FakeFocus((page([row]), Reply(body=payload))) as fake,
        patch.object(
            api, "_commit_page", side_effect=api.fiscal_ingest.StaleISSOriginalsError
        ) as commit,
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        try:
            run = api.synchronize(world.tenant_id, stream.pk)
        except api.fiscal_ingest.StaleISSOriginalsError as error:
            msg = "A second stale snapshot escaped instead of refusing the page."
            raise AssertionError(msg) from error
    assert commit.call_count == 2
    assert run.state == "bloqueada"
    assert world.reload(stream).committed_cursor == "0"
    assert fiscal_rows(world) == []
    assert len(fake.requests) == 2


def test_documents_before_the_capture_start_month_are_accounted_but_not_ingested(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream()
    rows = [
        item(1, emitted="2026-08-31T23:59:59-03:00"),
        item(2, index=2, emitted="2026-09-01T00:00:00-03:00"),
    ]
    with FakeFocus((page(rows),)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run.skipped_before_start == 1
    assert [row.document_key for row in fiscal_rows(world)] == [key(2)]
    assert world.reload(stream).committed_cursor == "2"
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("disablement_state", "pausa_solicitada"),
        ("credential_state", "divergente"),
        ("status", "revogada"),
        ("streams", []),
        ("disable_epoch", 1),
    ],
)
def test_page_commit_rechecks_live_eligibility_and_the_disable_epoch(
    world: CaptureWorld,
    settings: SettingsWrapper,
    field: str,
    value: object,
) -> None:
    api, stream = capture_api(), world.stream()
    prepare = api._prepare_page

    def disable_after_prepare(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        with tenant_context(world.tenant_id):
            ClientProviderLink.objects.filter(pk=world.link.pk).update(**{field: value})
        return prepared

    with FakeFocus((page([item(1, full=True)]), Reply(body=xml()))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        with patch.object(api, "_prepare_page", side_effect=disable_after_prepare):
            run = api.synchronize(world.tenant_id, stream.pk)
    assert run.state == "bloqueada"
    assert world.reload(stream).committed_cursor == "0"
    assert fiscal_rows(world) == []


@pytest.mark.parametrize("change", ["cursor_version", "run_fence", "expired_lease"])
def test_stale_cursor_fence_or_lease_cannot_commit_a_page(
    world: CaptureWorld, settings: SettingsWrapper, change: str
) -> None:
    api, stream = capture_api(), world.stream()
    prepare = api._prepare_page

    def supersede(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        with tenant_context(world.tenant_id):
            if change == "cursor_version":
                model("SyncStream").objects.filter(pk=stream.pk).update(
                    cursor_version=99
                )
            else:
                model("SyncRun").objects.filter(stream_id=stream.pk).update(
                    **(
                        {"fence": 99}
                        if change == "run_fence"
                        else {"lease_until": START - timedelta(seconds=1)}
                    )
                )
        return prepared

    with FakeFocus((page([item()]),)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        with patch.object(api, "_prepare_page", side_effect=supersede):
            api.synchronize(world.tenant_id, stream.pk)
    assert world.reload(stream).committed_cursor == "0"
    assert fiscal_rows(world) == []


@pytest.mark.parametrize("reason", ["global_pause", "focus_pause", "flag", "budget"])
def test_admission_refusals_send_nothing(
    world: CaptureWorld, settings: SettingsWrapper, reason: str
) -> None:
    api, stream = capture_api(), world.stream()
    if reason == "global_pause":
        settings.PROVIDERS_PAUSED = True
    elif reason == "focus_pause":
        settings.FOCUS_PAUSED = True
    elif reason == "flag":
        world.setup.graph.tenant.features = ["cycles_r1", "scan_required"]
        world.setup.graph.tenant.save(update_fields=["features"])
    else:
        with tenant_context(world.tenant_id):
            ProviderBudget.objects.filter(provider="focus").delete()
    with FakeFocus((page([item()]),)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, stream.pk)
    assert fake.requests == []
    assert world.reload(stream).committed_cursor == "0"
    assert fiscal_rows(world) == []


def test_reset_while_fetching_an_item_keeps_the_page_cursor_for_replay(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    replies = (
        page([item(1, full=True)]),
        Reply(reset=True),
        page([item(1, full=True)]),
        Reply(body=xml()),
    )
    with FakeFocus(replies) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        assert api.synchronize(world.tenant_id, stream.pk).state == "falhou"
        assert world.reload(stream).committed_cursor == "0"
        assert fiscal_rows(world) == []
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
    assert world.reload(stream).committed_cursor == "1"
    assert len(fiscal_rows(world)) == 1
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 1


def test_three_consecutive_failed_runs_open_exactly_one_capture_exception(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    with FakeFocus((Reply(reset=True),) * 3) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        for hour in range(3):
            monkeypatch.setattr(
                timezone, "now", lambda hour=hour: START + timedelta(hours=hour)
            )
            api.synchronize(world.tenant_id, stream.pk)
            with tenant_context(world.tenant_id):
                problems = CycleException.objects.filter(reason_code="falha_na_captura")
                assert problems.count() == int(hour == 2)
    stream = world.reload(stream)
    assert stream.status == "falha_na_captura"
    assert stream.consecutive_failures == 3
    assert stream.last_error_code
    assert stream.last_api_success_at is None
    assert stream.last_complete_scan_at is None
    with tenant_context(world.tenant_id):
        assert not IntegrationIssue.objects.filter(reason="falha_na_captura").exists()


def test_higher_cce_sequence_fetches_latest_correction_xml_once(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    correction = (
        '<procEventoNFe xmlns="http://www.portalfiscal.inf.br/nfe">'
        f"<evento><infEvento><chNFe>{key(1)}</chNFe><tpEvento>110110</tpEvento>"
        "<nSeqEvento>2</nSeqEvento><detEvento><xCorrecao>Synthetic correction"
        "</xCorrecao></detEvento></infEvento></evento></procEventoNFe>"
    ).encode()
    replies = (
        page([item(1, full=True, cce=2)]),
        Reply(body=xml()),
        Reply(body=correction),
        page([item(4, full=True, cce=2)]),
        Reply(body=xml()),
    )
    with FakeFocus(replies) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, stream.pk)
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
    assert (
        len([request for request in fake.requests if "carta_correcao" in request.path])
        == 1
    )
    with tenant_context(world.tenant_id):
        artifact = RevisionArtifact.objects.select_related("document").get(
            role="xml_cce"
        )
        assert artifact.document.portal_visible is False
        assert artifact.document.uploaded_by_id is None
    with default_storage.open(artifact.document.storage_key, "rb") as stored:
        assert stored.read() == correction
