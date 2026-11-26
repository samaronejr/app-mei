"""Frozen 0031 backfill, also rerunnable before enabling consumers after a revert."""

from itertools import groupby
from operator import attrgetter
from typing import Final

from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper

from apps.core.tenancy import tenant_context
from apps.obligations.fiscal_ingest import iss_old_fields_may_change
from apps.obligations.iss_retention_evidence_v1 import (
    EVIDENCE_UNAVAILABLE,
    ISSDecision,
    ISSOriginalRow,
    agreed_iss_code,
    current_iss_originals_page,
    verify_iss_original,
)
from apps.obligations.iss_retention_incidents import (
    ISSOutageIdentity,
    iss_evidence_key,
    reconcile_iss_incident,
)
from apps.obligations.iss_retention_v1 import project_iss_retention
from apps.obligations.models.fiscal import FiscalDocument

_PAGE_SIZE: Final = 100

_UPDATE_PARTY: Final = """
    UPDATE obligations_fiscaldocument
    SET iss_retention_party = %s
    WHERE id = %s AND tenant_id = %s AND client_id = %s
      AND current_revision_id IS NOT DISTINCT FROM %s
      AND iss_retention_party IS DISTINCT FROM %s
"""


class BackfillRefusedError(RuntimeError):
    """The run lacked the preconditions to produce a complete backfill."""


def _reconcile_incident(
    db: BaseDatabaseWrapper,
    source: ISSOriginalRow,
    decision: ISSDecision,
    evidence_key: str | ISSOutageIdentity,
) -> int:
    if source.fiscal_id is None:
        return 0
    with transaction.atomic(using=db.alias), tenant_context(source.tenant_id):
        fiscal = (
            FiscalDocument.objects.using(db.alias)
            .select_for_update(of=("self",))
            .select_related("current_revision")
            .get(pk=source.fiscal_id, client_id=source.client_id)
        )
        revision = fiscal.current_revision
        if revision is None or revision.pk != source.revision_id:
            return 0
        resolution = reconcile_iss_incident(
            fiscal, revision, decision, evidence_key=evidence_key
        )
        party, flag = project_iss_retention(resolution.decision.code)
        values: dict[str, object] = {
            "fiscal_state": resolution.state,
            "iss_retention_party": party,
        }
        if iss_old_fields_may_change(fiscal, revision, resolution):
            values["iss_withheld_flag"] = flag
            values["iss_withheld_amount"] = fiscal.iss_stated if flag is True else None
        changed = [
            name for name, value in values.items() if getattr(fiscal, name) != value
        ]
        if not changed:
            return 0
        for name in changed:
            setattr(fiscal, name, values[name])
        fiscal.version += 1
        fiscal.save(update_fields=[*changed, "version"])
        return 1


def backfill_iss_retention_party(db: BaseDatabaseWrapper) -> int:
    """Reconcile party and finish only owned ISS projections."""
    if not db.get_autocommit():
        message = "ISS party backfill requires autocommit for vault reads."
        raise BackfillRefusedError(message)
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        if cursor.fetchone() != (True,):
            message = "ISS party backfill requires the migration database role."
            raise BackfillRefusedError(message)
    updated = 0
    last_id = None
    while True:
        rows = current_iss_originals_page(db, last_id, _PAGE_SIZE)
        if not rows:
            return updated
        for last_id, originals in groupby(rows, key=attrgetter("fiscal_id")):
            first, *remaining = originals
            witnesses = [
                row.original
                for row in (first, *remaining)
                if row.original.document_id is not None
            ]
            verified = {
                witness: verify_iss_original(witness)
                for witness in witnesses
                if witness.usable()
            }
            decision = agreed_iss_code(witnesses, verified.__getitem__)
            if decision.cause or first.state == "incerta":
                key: str | ISSOutageIdentity = iss_evidence_key(witnesses)
                if decision.cause == EVIDENCE_UNAVAILABLE:
                    failed = iss_evidence_key(
                        witness
                        for witness, outcome in verified.items()
                        if outcome.cause == EVIDENCE_UNAVAILABLE
                    )
                    key = ISSOutageIdentity(
                        iss_evidence_key(witnesses, failed_key=failed), failed
                    )
                updated += _reconcile_incident(db, first, decision, key)
                continue
            party = project_iss_retention(decision.code)[0]
            if party == first.party:
                continue
            with db.cursor() as cursor:
                cursor.execute(
                    _UPDATE_PARTY,
                    [
                        party,
                        last_id,
                        first.tenant_id,
                        first.client_id,
                        first.revision_id,
                        party,
                    ],
                )
                updated += cursor.rowcount
