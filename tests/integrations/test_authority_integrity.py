"""Database boundaries protect immutable authorities even without service checks."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from django.apps import apps
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.obligations.models import AcquisitionOrigin, Document
from tests.integrations.test_authority import authority_api
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def as_migrator() -> Iterator[None]:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def test_authority_empty_reverse_preserves_the_shared_append_only_function() -> None:
    authority_api()
    with as_migrator():
        MigrationExecutor(connection).migrate([("integrations", "0001_initial")])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regprocedure('audit_reject_mutation()') IS NOT NULL"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute("SELECT to_regclass('integrations_authorityrecord')")
            assert cursor.fetchone() == (None,)
            cursor.execute(
                "SELECT to_regclass('integrations_providerconnection') IS NOT NULL"
            )
            assert cursor.fetchone() == (True,)


def test_populated_authority_reverse_refuses_before_removing_any_protection() -> None:
    api = authority_api()
    firm = make_firm("authority-rollback", client_count=1)
    firm.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk):
        record = api.declare_authority(
            firm.owner,
            firm.clients[0].pk,
            service="sitfis",
            code="00002",
            valid_from=date(2026, 1, 1),
            expected_version=0,
        )
    with as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate([("integrations", "0001_initial")])
    with tenant_context(firm.tenant.pk):
        assert (
            apps.get_model("integrations", "AuthorityRecord")
            .objects.filter(pk=record.pk)
            .exists()
        )


def test_the_database_refuses_a_second_root_for_the_same_authority() -> None:
    authority_api()
    firm = make_firm("authority-root", client_count=1)
    model = apps.get_model("integrations", "AuthorityRecord")
    with tenant_context(firm.tenant.pk):
        values = {
            "tenant": firm.tenant,
            "client": firm.clients[0],
            "kind": "procuracao_ecac",
            "service": "sitfis",
            "code": "00002",
            "status": "ativa",
            "valid_from": date(2026, 1, 1),
            "declared_by": firm.owner,
        }
        model.objects.create(**values)
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(**values)


def test_a_successor_cannot_cross_client_or_service() -> None:
    authority_api()
    firm = make_firm("authority-chain", client_count=2)
    model = apps.get_model("integrations", "AuthorityRecord")
    with tenant_context(firm.tenant.pk):
        values = {
            "tenant": firm.tenant,
            "client": firm.clients[0],
            "kind": "procuracao_ecac",
            "service": "sitfis",
            "code": "00002",
            "status": "ativa",
            "valid_from": date(2026, 1, 1),
            "declared_by": firm.owner,
        }
        first = model.objects.create(**values)
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(
                **{
                    **values,
                    "client": firm.clients[1],
                    "supersedes": first,
                    "version": 2,
                }
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(
                **{
                    **values,
                    "service": "pagtoweb",
                    "code": "00004",
                    "supersedes": first,
                    "version": 2,
                }
            )


def _root_values(firm: Firm) -> dict[str, object]:
    return {
        "tenant": firm.tenant,
        "client": firm.clients[0],
        "kind": "procuracao_ecac",
        "service": "sitfis",
        "code": "00002",
        "status": "ativa",
        "valid_from": date(2026, 1, 1),
        "declared_by": firm.owner,
    }


def test_each_revision_has_at_most_one_successor() -> None:
    authority_api()
    firm = make_firm("authority-successor", client_count=1)
    model = apps.get_model("integrations", "AuthorityRecord")
    with tenant_context(firm.tenant.pk):
        root = model.objects.create(**_root_values(firm))
        model.objects.create(**_root_values(firm), supersedes=root, version=2)
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(**_root_values(firm), supersedes=root, version=3)


@pytest.mark.parametrize(
    ("constraint", "change"),
    [
        ("authorityrecord_catalog_code", {"code": "00004"}),
        ("authorityrecord_version_follows_chain", {"version": 2}),
        ("authorityrecord_valid_range", {"valid_to": date(2025, 12, 31)}),
        ("authorityrecord_usable_has_start", {"valid_from": None}),
        ("authorityrecord_verified_is_active", {"status": "revogada", "verify": True}),
        ("authorityrecord_person_or_call", {"declared_by": None}),
    ],
)
def test_the_database_refuses_an_inconsistent_revision(
    constraint: str, change: dict[str, object]
) -> None:
    authority_api()
    firm = make_firm("authority-check", client_count=1)
    model = apps.get_model("integrations", "AuthorityRecord")
    values = {**_root_values(firm), **change}
    if values.pop("verify", False):
        values["verified_by"] = firm.owner
    with tenant_context(firm.tenant.pk):
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            model.objects.create(**values)
        assert constraint in str(refused.value)


def test_not_even_the_table_owner_can_rewrite_authority_or_termo_rows() -> None:
    authority_api()
    firm = make_firm("authority-owner-role", client_count=1)
    record_model = apps.get_model("integrations", "AuthorityRecord")
    termo_model = apps.get_model("integrations", "FirmAuthorization")
    with tenant_context(firm.tenant.pk):
        record = record_model.objects.create(**_root_values(firm))
        document = Document.objects.create(
            tenant=firm.tenant,
            client=firm.clients[0],
            original_filename="termo.xml",
            content_type="application/xml",
            byte_size=10,
            sha256="b" * 64,
            uploaded_by=firm.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        termo = termo_model.objects.create(
            tenant=firm.tenant,
            kind="termo_autorizacao_serpro",
            signed_document=document,
            submitted_at=timezone.now(),
            status="enviado",
        )
    statements = (
        (
            "UPDATE integrations_authorityrecord SET status = 'revogada' WHERE id = %s",
            record.pk,
        ),
        (
            "UPDATE integrations_firmauthorization SET status = 'aceito' WHERE id = %s",
            termo.pk,
        ),
    )
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            for statement, pk in statements:
                with (
                    pytest.raises(DatabaseError, match="append-only"),
                    transaction.atomic(),
                ):
                    cursor.execute(statement, [pk])
        finally:
            cursor.execute("SET ROLE app_runtime")


def test_one_active_issue_per_reason_and_service() -> None:
    authority_api()
    firm = make_firm("authority-issue", client_count=1)
    model = apps.get_model("integrations", "IntegrationIssue")
    values = {
        "tenant": firm.tenant,
        "client": firm.clients[0],
        "reason": "autorizacao_ausente",
        "service": "sitfis",
        "owner": firm.owner,
        "review_on": date(2026, 11, 10),
    }
    with tenant_context(firm.tenant.pk):
        model.objects.create(**values, state="resolvida")
        model.objects.create(**values)
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(**values, state="em_tratamento")
