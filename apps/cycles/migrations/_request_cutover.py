"""Frozen persistence and DDL for the atomic request-privacy cutover."""

from collections import defaultdict
from itertools import batched
from uuid import UUID

from django.apps.registry import Apps
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import F

from apps.cycles.migrations._request_history import (
    INTERNAL,
    REPORT,
    Event,
    GuideArtifacts,
    Request,
    classify,
)
from apps.cycles.migrations._request_lifetime import LOCK_VISIBILITY, UNLOCK_VISIBILITY

# Schema-owner metadata, not a request audience flag.
RECONCILED = "AXRM-049 supported request history reconciled v1"

# Keep the existing tenant and obligation restrictions. Excluding the request
# also hides its links/facts through the reference folds of obligations 0023.
POLICIES = """
ALTER POLICY cycles_evidencerequest_portal_client_isolation
ON public.cycles_evidencerequest USING (
    client_id = CASE WHEN state <> 'interno' AND (obligation_id IS NULL OR EXISTS (
        SELECT 1 FROM public.obligations_obligation obligation
        WHERE obligation.id = cycles_evidencerequest.obligation_id
    )) THEN NULLIF(current_setting('app.client_id', true), '')::uuid END
);

-- Plain INSERT without RETURNING must not let the FK distinguish guessing a
-- hidden request from a nonexistent one. Leave the link's USING clause alone.
ALTER POLICY cycles_evidencelink_portal_client_isolation
ON public.cycles_evidencelink WITH CHECK (
    client_id = CASE WHEN EXISTS (
        SELECT 1 FROM public.cycles_evidencerequest parent_request
        WHERE parent_request.id = cycles_evidencelink.request_id
    ) THEN NULLIF(current_setting('app.client_id', true), '')::uuid END
);
"""
REVERSE_POLICIES = """
ALTER POLICY cycles_evidencerequest_portal_client_isolation
ON public.cycles_evidencerequest USING (
    client_id = CASE WHEN (obligation_id IS NULL OR EXISTS (
        SELECT 1 FROM public.obligations_obligation obligation
        WHERE obligation.id = cycles_evidencerequest.obligation_id
    )) THEN NULLIF(current_setting('app.client_id', true), '')::uuid END
);
ALTER POLICY cycles_evidencelink_portal_client_isolation
ON public.cycles_evidencelink WITH CHECK (
    client_id = NULLIF(current_setting('app.client_id', true), '')::uuid
);
"""


def _artifacts(
    editor: BaseDatabaseSchemaEditor, ids: list[UUID], has_calls: bool
) -> dict[UUID, GuideArtifacts]:
    with editor.connection.cursor() as cursor:
        # The only pre-cutover official guide_issued linked fact is PGMEI.
        # Staff can separately assert guide_issued; that is not this marker.
        cursor.execute(
            """
            SELECT DISTINCT l.request_id FROM public.cycles_evidencelink l
            JOIN public.obligations_sourceobservation o
              ON (o.tenant_id,o.client_id,o.evidence_link_id)=(l.tenant_id,l.client_id,l.id)
            WHERE l.request_id=ANY(%s) AND o.kind='guide_issued' AND o.source_kind='official_api'
        """,
            [ids],
        )
        official = {pk for (pk,) in cursor.fetchall()}
        das: set[UUID] = set()
        if has_calls:
            cursor.execute(
                """
                SELECT DISTINCT l.request_id FROM public.cycles_evidencelink l
                JOIN public.obligations_document d
                  ON (d.tenant_id,d.client_id,d.id)=(l.tenant_id,l.client_id,l.document_id)
                JOIN public.integrations_providercall c
                  ON (c.tenant_id,c.client_id)=(d.tenant_id,d.client_id)
                 AND c.id::pg_catalog.text=d.acquisition_receipt_ref
                WHERE l.request_id=ANY(%s) AND d.acquisition_origin='provedor_federal'
                  AND c.provider='serpro' AND c.operation='SP-DAS'
            """,
                [ids],
            )
            das = {pk for (pk,) in cursor.fetchall()}
        return {
            pk: GuideArtifacts(
                system_das_receipt=pk in das, official_guide_observation=pk in official
            )
            for pk in das | official
        }


def _events(
    apps: Apps, alias: str, selected: dict[UUID, Request]
) -> dict[UUID, list[Event]]:
    events = apps.get_model("cycles", "CycleEvent")._base_manager.using(alias)
    history: dict[UUID, list[Event]] = defaultdict(list)
    for pk, tenant, client, *fields in (
        events.filter(
            aggregate_type="EvidenceRequest",
            aggregate_id__in=selected,
            client_id__in={r.client for r in selected.values()},
            tenant_id__in={r.tenant for r in selected.values()},
        )
        .order_by("aggregate_id", "aggregate_version", "pk")
        .values_list(
            "aggregate_id",
            "tenant_id",
            "client_id",
            "aggregate_version",
            "type",
            "payload",
            "actor_id",
            "cycle_id",
            "schema_version",
        )
    ):
        if (tenant, client) == (selected[pk].tenant, selected[pk].client):
            history[pk].append(Event(*fields))
    return history


def _cuts(
    apps: Apps, alias: str, selected: dict[UUID, Request]
) -> tuple[dict[str, list[tuple[int, str]]], set[tuple[UUID, UUID, UUID]]]:
    indexes = apps.get_model("cycles", "EvidenceIndex")._base_manager.using(alias)
    scoped = {(r.tenant, r.client, r.cycle, str(r.id)) for r in selected.values()}
    cuts: dict[str, list[tuple[int, str]]] = defaultdict(list)
    unreadable = set()
    for tenant, client, cycle, items in (
        indexes.filter(cycle_id__in={r.cycle for r in selected.values()})
        .values_list("tenant_id", "client_id", "cycle_id", "data__requests")
        .iterator()
    ):
        if not isinstance(items, list):
            unreadable.add((tenant, client, cycle))
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                unreadable.add((tenant, client, cycle))
            elif (tenant, client, cycle, item["id"]) in scoped:
                v, state = item.get("version"), item.get("state")
                cuts[item["id"]].append(
                    (
                        v if type(v) is int else 0,
                        state if isinstance(state, str) else "",
                    )
                )
    return cuts, unreadable


def _repair_batch(
    apps: Apps,
    editor: BaseDatabaseSchemaEditor,
    selected: dict[UUID, Request],
    has_calls: bool,
) -> None:
    alias = editor.connection.alias
    ids = list(selected)
    history = _events(apps, alias, selected)
    cuts, unreadable = _cuts(apps, alias, selected)
    markers = _artifacts(editor, ids, has_calls)
    report = apps.get_model("audit", "Event")
    restore, notes = [], []
    for pk, row in selected.items():
        decision = classify(
            row,
            history[pk],
            markers.get(pk),
            cuts[str(pk)],
            unreadable_snapshot=(row.tenant, row.client, row.cycle) in unreadable,
        )
        if decision is None:
            continue
        if decision.restore:
            restore.append(pk)
        notes.append(report(**decision.audit))
    if restore:
        req = apps.get_model("cycles", "EvidenceRequest")._base_manager.using(alias)
        req.filter(pk__in=restore).update(state=INTERNAL, version=F("version") + 1)
    if notes:
        report._base_manager.using(alias).bulk_create(notes)


def _repair(apps: Apps, editor: BaseDatabaseSchemaEditor) -> None:
    """Read producer facts while schema-owner fences exclude concurrent writers."""
    editor.execute(
        "LOCK TABLE public.cycles_cycleevent, public.cycles_evidenceindex, "
        "public.obligations_document, public.obligations_sourceobservation IN SHARE MODE"
    )
    with editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.to_regclass('public.integrations_providercall')"
        )
        has_calls = cursor.fetchone()[0] is not None
    if has_calls:
        editor.execute("LOCK TABLE public.integrations_providercall IN SHARE MODE")
    # CROSS_TENANT_OK: the migration covers all firms; evidence joins are scoped.
    req = apps.get_model("cycles", "EvidenceRequest")._base_manager.using(
        editor.connection.alias
    )
    rows = req.order_by("cycle_id", "pk").values_list(
        "pk", "tenant_id", "client_id", "cycle_id", "state", "version", "created_by_id"
    )
    for batch in batched(rows.iterator(chunk_size=256), 256, strict=False):
        selected = {r.id: r for r in (Request(*values) for values in batch)}
        _repair_batch(apps, editor, selected, has_calls)


def install(apps: Apps, editor: BaseDatabaseSchemaEditor) -> None:
    """Repair and protect before the first cutover transaction becomes visible."""
    editor.execute(
        "LOCK TABLE public.cycles_evidencerequest, public.cycles_evidencelink "
        "IN ACCESS EXCLUSIVE MODE"
    )
    with editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.obj_description(oid, 'pg_trigger') FROM pg_catalog.pg_trigger "
            "WHERE tgrelid='public.cycles_evidencerequest'::pg_catalog.regclass "
            "AND tgname='cycles_keep_internal_request' AND NOT tgisinternal"
        )
        installed = cursor.fetchone()
    if installed == (RECONCILED,):
        return
    if installed is not None:
        # A previously installed 0026 already refuses restoring a public-looking
        # row to interno. No portal query or application write can interleave.
        editor.execute(
            "ALTER TABLE public.cycles_evidencerequest DISABLE TRIGGER cycles_keep_internal_request"
        )
    _repair(apps, editor)
    if installed is None:
        editor.execute(LOCK_VISIBILITY)
    else:
        editor.execute(
            "ALTER TABLE public.cycles_evidencerequest ENABLE TRIGGER cycles_keep_internal_request"
        )
    # 0025 may already be recorded as the lookup-only preparation. Reconcile,
    # install the guards and switch BOTH RLS clauses under the same write fence.
    editor.execute(POLICIES)
    editor.execute(
        "COMMENT ON TRIGGER cycles_keep_internal_request ON public.cycles_evidencerequest IS %s",
        [RECONCILED],
    )


def remove_lifetime(_apps: Apps, editor: BaseDatabaseSchemaEditor) -> None:
    """Only 0025 owns removal, after its reverse privacy guard has passed."""
    editor.execute(REVERSE_POLICIES)
    editor.execute(UNLOCK_VISIBILITY)


def refuse_exposure(apps: Apps, editor: BaseDatabaseSchemaEditor) -> None:
    editor.execute("LOCK TABLE public.cycles_evidencerequest IN ACCESS EXCLUSIVE MODE")
    alias = editor.connection.alias
    if (
        apps.get_model("cycles", "EvidenceRequest")
        ._base_manager.using(alias)
        .filter(state=INTERNAL)
        .exists()
        or apps.get_model("audit", "Event")
        ._base_manager.using(alias)
        .filter(action=REPORT, object_type="EvidenceRequest")
        .exists()
    ):
        message = "Internal request history is protected; use a forward repair."
        raise IrreversibleError(message)
