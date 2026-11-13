"""AXRM-020's frozen schema, policy and append-only migration contract."""

from importlib import import_module

from django.db import migrations, models

from apps.core.migrations._operations import (
    DenyPortalAccess,
    EnablePortalClientRLS,
    EnableRLS,
)
from apps.obligations.models import FiscalDocument, FiscalRevision, RevisionArtifact


def test_register_schema_keeps_scope_identity_and_money_contracts() -> None:
    assert FiscalDocument._meta.default_manager_name == "objects"
    assert FiscalDocument._meta.base_manager_name == "all_objects"
    for model in (FiscalDocument, FiscalRevision, RevisionArtifact):
        assert model._meta.get_field("client").null is False
        assert {("tenant", "client", "id")} <= {
            tuple(constraint.fields)
            for constraint in model._meta.constraints
            if isinstance(constraint, models.UniqueConstraint)
        }
    identity = next(
        c
        for c in FiscalDocument._meta.constraints
        if isinstance(c, models.UniqueConstraint)
        and c.name == "fiscaldocument_identity_uniq"
    )
    assert identity.fields == (
        "tenant",
        "client",
        "environment",
        "family",
        "key_namespace",
        "document_key",
    )
    for name in ("total", "iss_stated", "iss_withheld_amount"):
        field = FiscalDocument._meta.get_field(name)
        assert isinstance(field, models.DecimalField)
        assert (field.max_digits, field.decimal_places, field.null) == (14, 2, True)


def test_migration_declares_all_policy_and_append_only_operations() -> None:
    module = import_module("apps.obligations.migrations.0029_fiscal_register")
    operations = module.Migration.operations
    names = {"FiscalDocument", "FiscalRevision", "RevisionArtifact"}
    policies = [
        op
        for op in operations
        if isinstance(op, (EnableRLS, EnablePortalClientRLS, DenyPortalAccess))
    ]
    for kind in (EnableRLS, EnablePortalClientRLS, DenyPortalAccess):
        assert {op.model_name for op in policies if type(op) is kind} == names
    sql = "\n".join(
        str(op.sql) for op in operations if isinstance(op, migrations.RunSQL)
    )
    assert "obligations_fiscalrevision_append_only" in sql
    assert "obligations_revisionartifact_append_only" in sql
    assert "FOREIGN KEY" in sql
    assert isinstance(operations[-1], migrations.RunPython)
    assert operations[-1].reverse_code is module.refuse_reverse_with_rows
