"""Exercise ISS backfill and its maintenance and staff entry points."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from django.core.files.storage import default_storage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    ExtractedField,
    FieldOrigin,
    IntakeState,
)
from apps.cycles.records import Exception as CycleException
from apps.cycles.retention_records import ContentPurgeClaim
from apps.obligations.models import FiscalDocument, FiscalRevision, RevisionArtifact
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import (
    KEY,
    LEGACY_ISS_DIGEST,
    UNTRUSTED_ISS_ELEMENTS,
    nfse_with_retention,
    retention_element,
)
from tests.obligations.iss_backfill_fixtures import (
    ADD_ISS_PARTY,
    BEFORE_ISS_PARTY,
    _ingest_retention_element,
    _ingest_retention_note,
    _key,
    _payload,
    at_0029,
    migration_role,
    run_iss_catch_up,
    seed_legacy_code_two_heads,
)
from tests.obligations.iss_vault_fixture import ISS_VAULT_FAULTS, fault_iss_original
from tests.obligations.test_fiscal_documents import (
    _STATED,
    _UNTRUSTED_XML_PARAMS,
    _incidents,
    _iss_projection,
    _source_document,
    document,
    ingest,
    revision_of,
)
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup

type ISSProjection = tuple[str, bool | None, Decimal | None]
type ISSConsensusCase = tuple[str, tuple[bytes, ...], ISSProjection]


@pytest.mark.parametrize(
    ("code", "label"),
    [
        ("1", "ISS não retido"),
        ("2", "ISS retido pelo tomador"),
        ("3", "ISS retido pelo intermediário"),
        ("9", "Retenção do ISS desconhecida"),
    ],
)
def test_iss_retention_party_is_shown_on_the_staff_review(
    setup: PortalSetup, settings: SettingsWrapper, code: str, label: str
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    fiscal = _ingest_retention_note(setup, code)
    response = setup.firm().get(reverse("document-review", args=[fiscal.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    assert label in body
    if code == "3":
        assert "ISS retido pelo tomador" not in body


_ISS_SPAN = "DPS/infDPS/valores/trib/tribMun/tpRetISSQN"
_ISS_REDACTED_AT = datetime(2026, 11, 22, tzinfo=UTC)


def _legacy_tuple(code: str | None) -> tuple[bool | None, Decimal | None]:
    return {"1": (False, None), "2": (True, _STATED), "3": (True, _STATED)}.get(
        code or "", (None, None)
    )


def _retain_legacy_iss_field(fiscal: FiscalDocument, code: str) -> None:
    ExtractedField.objects.get_or_create(
        tenant_id=fiscal.tenant_id,
        client_id=fiscal.client_id,
        document=_source_document(fiscal),
        name="iss_withheld_flag",
        origin=FieldOrigin.XML,
        extractor_version="2",
        defaults={"value": code, "source_span": _ISS_SPAN},
    )


def _stored_iss_history() -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, current_revision_id, version, iss_withheld_flag, "
            "iss_withheld_amount FROM obligations_fiscaldocument ORDER BY id"
        )
        return list(cursor.fetchall())


def _stored_iss_parties() -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, iss_retention_party, ctid::text "
            "FROM obligations_fiscaldocument ORDER BY id"
        )
        return list(cursor.fetchall())


def _seed_legacy_iss_table(setup: PortalSetup) -> dict[object, str]:
    cases = [
        (retention_element("1"), "1", "nao_retido"),
        (retention_element("2"), "2", "tomador"),
        (retention_element("3"), "3", "intermediario"),
        *[
            (element, code, "desconhecido")
            for _, element, code in UNTRUSTED_ISS_ELEMENTS
        ],
    ]
    expected: dict[object, str] = {}
    for index, (element, code, party) in enumerate(cases):
        for unavailable in (False, True):
            fiscal = _ingest_retention_element(
                setup, element, key=_key(index * 2 + unavailable)
            )
            flag, amount = _legacy_tuple(code)
            with tenant_context(fiscal.tenant_id):
                if code is not None:
                    _retain_legacy_iss_field(fiscal, code)
                if unavailable:
                    ExtractedField.objects.filter(
                        document=_source_document(fiscal), name="iss_withheld_flag"
                    ).update(value="", source_span="", redacted_at=_ISS_REDACTED_AT)
                    # Distinguish exact zero and NULL even for incomplete snapshots.
                    if code == "3" or code is None:
                        amount = Decimal("0.00")
                FiscalDocument.objects.filter(pk=fiscal.pk).update(
                    iss_withheld_flag=flag, iss_withheld_amount=amount
                )
            expected[fiscal.pk] = "desconhecido" if unavailable else party
    return expected


def test_iss_retention_populated_round_trip_preserves_all_legacy_values(
    setup: PortalSetup,
) -> None:
    expected = _seed_legacy_iss_table(setup)
    with migration_role(), at_0029() as after:
        original = _stored_iss_history()
        MigrationExecutor(connection).migrate(after)
        backfilled = _stored_iss_parties()
        # Run the actual reverse even when a bad forward run modified old data.
        modified = _stored_iss_history()
        MigrationExecutor(connection).migrate([BEFORE_ISS_PARTY])
        restored = _stored_iss_history()
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "obligations_fiscaldocument"
            )
    assert "iss_retention_party" not in {column.name for column in columns}
    assert ({row[0]: row[1] for row in backfilled}, modified, restored) == (
        expected,
        original,
        original,
    )


def test_iss_retention_backfill_uses_only_exact_current_retained_scope(
    setup: PortalSetup,
) -> None:
    foreign = portal_setup("iss-foreign")
    foreign.graph.tenant.features = ["cycles_r1", "scan_required", "document_capture"]
    foreign.graph.tenant.save(update_fields=["features"])
    other = _ingest_retention_note(foreign, "3", key=_key(101))
    stale = _ingest_retention_note(setup, "2", key=_key(101))
    current = _ingest_retention_note(setup, None, key=_key(101), version=2)
    assert current.current_revision_id != stale.current_revision_id
    no_bytes = _ingest_retention_note(setup, "2", key=_key(102))
    claimed = _ingest_retention_note(setup, "3", key=_key(103))
    wrong_bytes = _ingest_retention_note(setup, "1", key=_key(104))
    no_field = _ingest_retention_note(setup, "2", key=_key(105))
    not_verified = _ingest_retention_note(setup, "2", key=_key(106))
    with tenant_context(current.tenant_id):
        document_id = _source_document(current).pk
        for origin, version in [(FieldOrigin.STAFF, "2"), (FieldOrigin.XML, "1")]:
            ExtractedField.objects.create(
                tenant_id=current.tenant_id,
                client_id=current.client_id,
                document_id=document_id,
                name="iss_withheld_flag",
                value="2",
                origin=origin,
                source_span=_ISS_SPAN,
                extractor_version=version,
            )
        claimed_original = _source_document(claimed)
        # Leave a one-key descriptor so the claim does not depend on collation
        # ordering a randomized final key against the quarantine prefix.
        DocumentIntake.objects.filter(document=claimed_original).update(
            quarantine_key=""
        )
        ContentPurgeClaim.objects.create(
            tenant_id=claimed.tenant_id,
            client_id=claimed.client_id,
            content_kind="document",
            content_id=claimed_original.pk,
            storage_keys=[claimed_original.storage_key],
        )
        missing_key = _source_document(no_bytes).storage_key
        wrong_key = _source_document(wrong_bytes).storage_key
        ExtractedField.objects.filter(
            document=_source_document(no_field), name="iss_withheld_flag"
        ).update(extractor_version="1")
        DocumentIntake.objects.filter(document=_source_document(not_verified)).update(
            state=IntakeState.QUARANTINED
        )
    default_storage.delete(missing_key)
    with default_storage.open(wrong_key, "wb") as stream:
        payload = _payload(retention_element("1"), _key(104))
        stream.write(payload.replace(b"12.34", b"42.34"))

    with migration_role(), at_0029() as after:
        MigrationExecutor(connection).migrate(after)
        actual = {row[0]: row[1] for row in _stored_iss_parties()}
    assert actual == {
        other.pk: "intermediario",
        current.pk: "desconhecido",
        no_bytes.pk: "desconhecido",
        claimed.pk: "desconhecido",
        wrong_bytes.pk: "desconhecido",
        no_field.pk: "desconhecido",
        not_verified.pk: "desconhecido",
    }


def test_iss_retention_backfill_requires_agreement_across_current_originals(
    setup: PortalSetup,
) -> None:
    expected = {}
    for index, (extra, code, redacted, party) in enumerate(
        [
            (retention_element("2"), "2", False, "tomador"),
            (retention_element("3"), "3", False, "desconhecido"),
            (retention_element(" 2 "), "2", False, "desconhecido"),
            (retention_element("2"), "2", True, "desconhecido"),
        ],
        201,
    ):
        current = _ingest_retention_note(setup, "2", key=_key(index), version=2)
        current_revision_id = current.current_revision_id
        assert current_revision_id is not None
        additional = ingest(
            setup,
            payload=_payload(extra, _key(index)) + b"\n",
            version=1,
            client_role="tomador",
            source_namespace="focus:nfse_recebidas",
        )
        assert isinstance(additional, FiscalDocument)
        with tenant_context(current.tenant_id):
            stale = current.revisions.get(provider_version="1")
            original = RevisionArtifact.objects.get(revision=stale).document
            # Model a second legacy artifact, including ambiguity the lossy
            # extraction could not encode in the current revision's digest.
            RevisionArtifact.objects.create(
                tenant_id=current.tenant_id,
                client_id=current.client_id,
                revision_id=current_revision_id,
                document=original,
                role="xml_original",
            )
            ExtractedField.objects.get_or_create(
                tenant_id=current.tenant_id,
                client_id=current.client_id,
                document=original,
                name="iss_withheld_flag",
                origin=FieldOrigin.XML,
                extractor_version="2",
                defaults={"source_span": _ISS_SPAN, "value": code},
            )
            if redacted:
                # A redaction marker forbids use even if an old token survives it.
                ExtractedField.objects.filter(document=original).update(
                    redacted_at=_ISS_REDACTED_AT
                )
        expected[current.pk] = party
    with migration_role(), at_0029() as after:
        MigrationExecutor(connection).migrate(after)
        actual = {row[0]: row[1] for row in _stored_iss_parties()}
    assert actual == expected


def _iss_consensus_cases() -> list[ISSConsensusCase]:
    one, two, three = (retention_element(code) for code in ("1", "2", "3"))
    unknown = ("desconhecido", None, None)
    malformed = {name: element for name, element, _ in UNTRUSTED_ISS_ELEMENTS}
    return [
        ("one", (one,), ("nao_retido", False, None)),
        ("two", (two,), ("tomador", True, _STATED)),
        ("three", (three,), ("intermediario", True, _STATED)),
        *((name, (element,), unknown) for name, element in malformed.items()),
        (
            "equivalent",
            (
                two,
                b'<tpRetISSQN xmlns="http://www.sped.fazenda.gov.br/nfse">2</tpRetISSQN>',
            ),
            ("tomador", True, _STATED),
        ),
        ("clean-then-padded", (two, malformed["padded"]), unknown),
        ("padded-then-clean", (malformed["padded"], two), unknown),
        ("clean-then-duplicate", (two, malformed["same-duplicate"]), unknown),
        ("clean-then-mixed", (one, malformed["mixed"]), unknown),
        ("clean-then-nbsp", (one, malformed["nbsp"]), unknown),
        ("absent-and-empty", (malformed["absent"], malformed["empty"]), unknown),
        (
            "unknown-and-padded",
            (malformed["unknown"], malformed["padded-unknown"]),
            unknown,
        ),
    ]


def _register_iss_consensus_case(
    setup: PortalSetup, elements: tuple[bytes, ...], key: str
) -> tuple[FiscalDocument, ISSProjection]:
    first = _ingest_retention_element(setup, elements[0], key=key)
    revision = revision_of(first)
    for element in elements[1:]:
        current = _ingest_retention_element(setup, element, key=key)
        assert current.current_revision_id == revision.pk
        assert current.fiscal_state == "autorizada"
    with tenant_context(first.tenant_id):
        before = FiscalDocument.objects.get(pk=first.pk)
        artifacts = RevisionArtifact.objects.filter(
            revision=revision, role="xml_original"
        )
        assert artifacts.count() == len(elements)
    for element in elements:
        replay = _ingest_retention_element(setup, element, key=key)
        assert replay.current_revision_id == revision.pk
        assert revision_of(replay).semantic_digest == revision.semantic_digest
        assert replay.version == before.version
        assert _iss_projection(replay) == _iss_projection(before)
    with tenant_context(first.tenant_id):
        assert artifacts.count() == len(elements)
        assert FiscalRevision.objects.filter(document=first).count() == 1
        assert not CycleException.objects.filter(
            reason_code="incidente_consistencia_fiscal"
        ).exists()
    return first, _iss_projection(before)


@pytest.mark.parametrize("fault", ISS_VAULT_FAULTS)
@pytest.mark.parametrize("reverse_order", [False, True], ids=["forward", "reverse"])
def test_iss_retention_four_paths_share_current_original_vault_failure(
    setup: PortalSetup, fault: str, reverse_order: bool
) -> None:
    elements = (
        retention_element("2"),
        b'<tpRetISSQN xmlns="http://www.sped.fazenda.gov.br/nfse">2</tpRetISSQN>',
    )
    first, second = reversed(elements) if reverse_order else elements
    initial = _ingest_retention_element(setup, first)
    revision = revision_of(initial)
    with tenant_context(initial.tenant_id):
        source = _source_document(initial)

    payload = nfse_with_retention(first)
    with fault_iss_original(fault, source.storage_key, payload):
        attached = _ingest_retention_element(setup, second)
        replayed = _ingest_retention_element(setup, second)
        assert replayed.version == attached.version
        assert _incidents(attached) == ["evidence_unavailable"]

    _assert_iss_retry_recovers(setup, second, initial)
    with fault_iss_original(fault, source.storage_key, payload):
        replay_only = _ingest_retention_element(setup, first)
        assert _incidents(replay_only) == ["evidence_unavailable"] * 2
    _assert_iss_retry_recovers(setup, second, initial)

    with fault_iss_original(fault, source.storage_key, payload):
        with migration_role(), at_0029() as after:
            MigrationExecutor(connection).migrate(after)
            backfilled = document(setup)
        assert _incidents(backfilled) == ["evidence_unavailable"] * 3
    _assert_iss_catch_up_recovers(setup)

    with fault_iss_original(fault, source.storage_key, payload):
        with migration_role():
            run_iss_catch_up()
        caught_up = document(setup)
        assert _incidents(caught_up) == ["evidence_unavailable"] * 4
        with migration_role():
            run_iss_catch_up()
        assert document(setup).version == caught_up.version
    _assert_iss_catch_up_recovers(setup)

    for result in (attached, replayed, replay_only, backfilled, caught_up):
        assert result.fiscal_state == "incerta"
        assert _iss_projection(result) == ("desconhecido", True, _STATED)
        assert result.current_revision_id == revision.pk
    assert _incidents(initial) == ["evidence_unavailable"] * 4
    with tenant_context(initial.tenant_id):
        assert (
            CycleException.objects.filter(
                reason_code="incidente_consistencia_fiscal", subject_ref=initial.pk
            ).count()
            == 1
        )
        assert FiscalRevision.objects.filter(document=initial).count() == 1
        assert RevisionArtifact.objects.filter(revision=revision).count() == 2


def _assert_iss_retry_recovers(
    setup: PortalSetup, element: bytes, first: FiscalDocument
) -> None:
    restored = _ingest_retention_element(setup, element)
    assert restored.current_revision_id == first.current_revision_id
    assert restored.fiscal_state == "autorizada"
    assert _iss_projection(restored) == ("tomador", True, _STATED)
    assert _ingest_retention_element(setup, element).version == restored.version


def _assert_iss_catch_up_recovers(setup: PortalSetup) -> None:
    with migration_role():
        run_iss_catch_up()
    recovered = document(setup)
    assert recovered.fiscal_state == "autorizada"
    assert _iss_projection(recovered) == ("tomador", True, _STATED)
    with migration_role():
        run_iss_catch_up()
    assert document(setup).version == recovered.version


def test_iss_retention_four_paths_share_current_original_consensus(
    setup: PortalSetup,
) -> None:
    expected = {}
    projections = {}
    for idx, (label, elements, result) in enumerate(_iss_consensus_cases(), 600):
        key = _key(idx)
        first, attached = _register_iss_consensus_case(setup, elements, key)
        if len(elements) == 1:
            fresh = first
        else:
            # Make an unrelated namespace current. On restoring the old head,
            # this same set must drive fresh projection, not just same-head replay.
            other = ingest(
                setup,
                payload=_payload(retention_element("3"), key),
                client_role="tomador",
                source_namespace="different:fiscal_namespace",
            )
            assert isinstance(other, FiscalDocument)
            assert other.current_revision_id != first.current_revision_id
            fresh = _ingest_retention_element(setup, elements[0], key=key)
            assert fresh.current_revision_id == first.current_revision_id
        expected[first.pk] = (label, result)
        projections[first.pk] = (attached, _iss_projection(fresh))

    with migration_role(), at_0029() as after:
        MigrationExecutor(connection).migrate(after)
        backfilled = _current_iss_tuples(setup)
    with tenant_context(setup.graph.tenant.pk):
        for fiscal_id, (_, result) in expected.items():
            wrong = "nao_retido" if result[0] == "desconhecido" else "desconhecido"
            FiscalDocument.objects.filter(pk=fiscal_id).update(
                iss_retention_party=wrong
            )
    with migration_role():
        run_iss_catch_up()
    caught_up = _current_iss_tuples(setup)
    for fiscal_id, (label, result) in expected.items():
        assert (
            *projections[fiscal_id],
            backfilled[fiscal_id],
            caught_up[fiscal_id],
        ) == (result, result, result, result), label


def _current_iss_tuples(setup: PortalSetup) -> dict[UUID, ISSProjection]:
    with tenant_context(setup.graph.tenant.pk):
        return {
            fiscal.pk: _iss_projection(fiscal)
            for fiscal in FiscalDocument.objects.all()
        }


def test_iss_retention_catch_up_reaches_the_101st_head(setup: PortalSetup) -> None:
    with migration_role(), at_0029():
        ids = seed_legacy_code_two_heads(setup, map(_key, range(101)))
        MigrationExecutor(connection).migrate([ADD_ISS_PARTY])
        assert _current_iss_tuples(setup) == dict.fromkeys(
            ids, ("desconhecido", True, _STATED)
        )
        run_iss_catch_up()
        actual = _current_iss_tuples(setup)
        expected = dict.fromkeys(ids, ("tomador", True, _STATED))
        assert len(expected) == 101
        assert actual == expected


def test_iss_retention_catch_up_reprojects_both_defaulted_and_advanced_rows(
    setup: PortalSetup,
) -> None:
    advanced = _ingest_retention_note(setup, "1", key=_key(301))
    created = _ingest_retention_note(setup, "2", key=_key(302))
    latest = _ingest_retention_note(setup, "3", key=_key(301), version=2)
    assert latest.current_revision_id != advanced.current_revision_id
    old_writer = (
        MigrationExecutor(connection)
        .loader.project_state([BEFORE_ISS_PARTY])
        .apps.get_model("obligations", "FiscalDocument")
    )
    with tenant_context(advanced.tenant_id):
        FiscalDocument.objects.filter(pk=advanced.pk).update(
            current_revision_id=advanced.current_revision_id,
            iss_retention_party="nao_retido",
            iss_withheld_flag=False,
            iss_withheld_amount=None,
        )
        FiscalDocument.objects.filter(pk=created.pk).update(
            iss_retention_party="desconhecido"
        )
        row = old_writer._base_manager.get(pk=advanced.pk)
        row.current_revision_id = latest.current_revision_id
        row.iss_withheld_flag, row.iss_withheld_amount = (True, _STATED)
        row.save()  # Pre-0030 writer still lists every old column, no party.
        assert (
            FiscalDocument.objects.get(pk=advanced.pk).iss_retention_party
            == "nao_retido"
        )
    with migration_role():
        history = _stored_iss_history()
        run_iss_catch_up()
        backfilled = _stored_iss_parties()
        run_iss_catch_up()
        assert _stored_iss_parties() == backfilled
        assert _stored_iss_history() == history
    assert {row[0]: row[1] for row in backfilled} == {
        advanced.pk: "intermediario",
        created.pk: "tomador",
    }


@pytest.mark.parametrize(
    ("element", "legacy_code"),
    _UNTRUSTED_XML_PARAMS,
)
def test_iss_retention_replay_reuses_legacy_revision_until_an_actual_change(
    setup: PortalSetup, element: bytes, legacy_code: str | None
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        fiscal = FiscalDocument.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            environment="fake",
            family="nfse_nacional",
            key_namespace="nacional",
            document_key=f"NFS{KEY}",
            client_role="tomador",
            source="captura",
            provider="focus",
            fiscal_state="autorizada",
        )
        established = FiscalRevision.objects.create(
            tenant_id=fiscal.tenant_id,
            client_id=fiscal.client_id,
            document=fiscal,
            source_namespace="focus:nfse_recebidas",
            provider_version="1",
            semantic_digest=LEGACY_ISS_DIGEST[legacy_code],
            fiscal_state="autorizada",
        )
        fiscal.current_revision = established
        fiscal.save(update_fields=["current_revision"])

    _ingest_retention_element(setup, element)
    current = document(setup)
    with tenant_context(fiscal.tenant_id):
        revisions = list(FiscalRevision.objects.values_list("pk", "semantic_digest"))
        assert revisions == [(established.pk, LEGACY_ISS_DIGEST[legacy_code])]
        assert not CycleException.objects.filter(
            reason_code="incidente_consistencia_fiscal"
        ).exists()
        assert RevisionArtifact.objects.filter(revision=established).count() == 1
    assert current.fiscal_state == "autorizada"
    assert current.version == fiscal.version

    _ingest_retention_note(setup, "3")
    current = document(setup)
    assert current.current_revision_id == established.pk
    assert current.fiscal_state == "incerta"
    with tenant_context(fiscal.tenant_id):
        assert FiscalRevision.objects.count() == 1
        assert (
            CycleException.objects.filter(
                reason_code="incidente_consistencia_fiscal"
            ).count()
            == 1
        )
