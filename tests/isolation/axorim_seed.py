"""One consistent tenant graph per firm, seeded for every model the sweep covers.

T26 needs one row per model per tenant — not a realistic cycle, just a row that
satisfies every NOT NULL column and every composite foreign key — so that "tenant B's
row is invisible" is never passing because there was nothing to hide. Seeding is
generic (introspected fields plus a small override table) rather than forty
hand-written builders, because the *enumeration*, not the shape of any one row, is
the thing under test.

Two rules keep the graph honest:

* Every foreign key resolves INSIDE the same `(tenant, client)` pair, which is what
  the `*_tenant_client_fk` / `*_pair_fk` composites require — a cross-client parent
  would fail at seed time, not in the assertion.
* "Twin" columns (the digest/revision an exact composite repeats from its parent)
  are copied from the seeded parent via `_sync_twin_fields`, never invented.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.test import Client

    from apps.cycles.effects.manifest import JSONValue

import hashlib
import json

import uuid6
from django.apps import apps as django_apps
from django.db import connection, models
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.models import TenantScopedModel
from apps.core.tenancy import client_context, tenant_context
from apps.cycles.effects.manifest import ActionKind
from apps.tenants.models import Membership, Tenant, TenantRole

PERIOD = dt.date(2026, 9, 1)

# The Axorim surface this stage sweeps: every model in the four apps the pivot
# created whole, plus the pivot's additions to the pre-existing client and
# obligation apps. Explicit by name — a model added to any of these apps without
# updating this list fails `test_the_explicit_list_matches_the_registry`.
AXORIM_APP_LABELS = ("cycles", "delivery", "briefing", "automation")
NEW_CLIENT_MODELS = (
    "ClientContact",
    "Engagement",
    "ImportBatch",
    "ImportRow",
    "TaxProfile",
)
NEW_OBLIGATION_MODELS = ("SourceObservation",)

# Client-scoped rows whose ONLY client-bearing constraint is the (tenant, client)
# pair itself: swapping their client_id is schema-legal, so the cross-client probe
# does not apply to them. An entry here is a decision, not a convenience — a model
# with a parent-scoped composite belongs on the probe side instead.
CLIENT_PAIR_ONLY_MODELS = frozenset(
    {
        "clients_engagement",
        "cycles_briefingfeedback",
        "cycles_contentpurgeclaim",
        "cycles_legalhold",
    }
)


@dataclass
class SeedContext:
    """Everything a row needs: its tenant, its client, a user, and the memo."""

    tenant: Tenant
    client: ClientCompany
    user: User
    tag: str
    rows: dict[str, models.Model] = dc_field(default_factory=dict)


def axorim_models() -> list[type[models.Model]]:
    """Every model the sweep covers, in dependency-friendly order."""
    selected: list[type[models.Model]] = []
    for label in AXORIM_APP_LABELS:
        selected.extend(django_apps.get_app_config(label).get_models())
    selected.extend(
        django_apps.get_model("clients", name) for name in NEW_CLIENT_MODELS
    )
    selected.extend(
        django_apps.get_model("obligations", name) for name in NEW_OBLIGATION_MODELS
    )
    return selected


def _first_choice(field: models.Field[Any, Any]) -> object | None:
    choices = list(field.choices or [])
    if not choices:
        return None
    first: object = choices[0]
    if isinstance(first, (tuple, list)) and isinstance(first[0], (tuple, list)):
        return cast("object", first[0][0])
    if isinstance(first, (tuple, list)):
        return cast("object", first[0])
    return first


def _manager(model: type[models.Model]) -> models.Manager[models.Model]:
    """`default_manager` is all_objects on scoped models — ContextVar-free."""
    manager = model._meta.default_manager
    assert manager is not None
    return cast("models.Manager[models.Model]", cast("object", manager))


def _string_default(field: models.Field[Any, Any], ctx: SeedContext) -> object:
    """The character-column branch, kept apart so `_default_for` stays readable."""
    if isinstance(field, models.EmailField):
        return f"{ctx.tag}@example.test"
    if isinstance(field, models.FileField):
        return "seed/file.bin"
    max_length = getattr(field, "max_length", 40) or 40
    name = field.name
    if "sha256" in name or "digest" in name:
        return "a" * 64
    if name == "cnpj":
        return "11222333000181" if ctx.tag.endswith("a") else "11444777000161"
    return f"{ctx.tag}-{name}"[:max_length]


# Required-by-constraint-but-nullable columns the generic seeder leaves blank.
_EXTRA_DEFAULTS: dict[str, dict[str, object]] = {
    "cycles.Exception": {
        "due_on": dt.date(2026, 10, 1),
        # The portal predicate is client_visible-folded; a hidden row counts as
        # zero regardless of tenant, which would fake a leak signal.
        "client_visible": True,
        "client_message": "seed visible",
    },
    "cycles.EvidenceRequest": {
        # The kind/obligation CHECK admits exactly two shapes; 'documento' is the
        # one a generic seed can satisfy without inventing an obligation row.
        "kind": "documento",
    },
    "tenants.BillingAccount": {
        # The account page renders next_invoice_on|data unconditionally.
        "next_invoice_on": PERIOD,
    },
    "cycles.DocumentIntake": {
        # document-preview only serves clean states; 'recebido' is quarantine.
        "state": "verificado",
    },
}

# Rows whose insert fires a domain trigger the generic kwargs cannot satisfy.
_SPECIAL_SEEDERS: dict[str, str] = {
    "cycles.ActionProposal": "_seed_action_proposal",
    "cycles.ContentPurgeClaim": "_seed_purge_claim",
    "cycles.ClientPublication": "_seed_client_publication",
    "cycles.BulkOperation": "_seed_bulk_operation",
    "cycles.EvidenceLink": "_seed_evidence_link",
    "tenants.BillingAccount": "_seed_billing_account",
}

_SCALAR_BY_KIND: dict[str, object] = {
    "DateField": PERIOD,
    "DecimalField": Decimal("100.00"),
    "IntegerField": 1,
    "BigIntegerField": 1,
    "PositiveIntegerField": 1,
    "PositiveBigIntegerField": 1,
    "PositiveSmallIntegerField": 1,
    "SmallIntegerField": 1,
    "FloatField": 1.0,
    "BooleanField": False,
    "JSONField": {},
    "BinaryField": b"{}",
}


def _scalar_default(field: models.Field[Any, Any]) -> object:
    """The scalar-column branches of `_default_for`."""
    if isinstance(field, models.DateTimeField):
        return timezone.now() + dt.timedelta(days=7)
    if isinstance(field, models.UUIDField):
        return uuid6.uuid7()
    result: object = _SCALAR_BY_KIND.get(field.get_internal_type())
    return result


def _default_for(field: models.Field[Any, Any], ctx: SeedContext) -> object:
    """A plausible value for one required column."""
    choice = _first_choice(field)
    if choice is not None:
        return choice
    scalar = _scalar_default(field)
    if scalar is not None:
        return scalar
    return _string_default(field, ctx)


def seed_model(
    model: type[models.Model],
    ctx: SeedContext,
    *,
    overrides: dict[str, object] | None = None,
) -> models.Model:
    """Insert one row of `model` inside ctx's (tenant, client), memoized."""
    key = model._meta.label_lower
    if key in ctx.rows:
        return ctx.rows[key]

    special = _SPECIAL_SEEDERS.get(model._meta.label)
    if special is not None:
        seeder = cast(
            "Callable[[type[models.Model], SeedContext], models.Model]",
            globals()[special],
        )
        instance = seeder(model, ctx)
        ctx.rows[key] = instance
        return instance

    kwargs: dict[str, object] = {}
    self_fk = _collect_fields(model, kwargs, ctx)
    kwargs.update(overrides or {})
    # Constraints a blank field cannot express (e.g. cycles.Exception needs a
    # review or due date). Keep this table to what a CHECK actually enforces.
    for name, value in _EXTRA_DEFAULTS.get(model._meta.label, {}).items():
        kwargs.setdefault(name, value)

    # The twin columns an exact composite repeats from its parent must match the
    # seeded parent's values, not fresh defaults (e.g. approval_exact_manifest_fk).
    _sync_twin_fields(model, kwargs)

    manager = _manager(model)
    # The insert runs under the row's own tenant+client GUCs: RLS checks WITH
    # CHECK on INSERT, cycles_link_retention reads app.client_id, and the whole
    # point of the sweep is that app_test does not bypass any of it.
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        instance = manager.create(**kwargs)
    ctx.rows[key] = instance
    if self_fk is not None:
        _link_self_parent(model, self_fk, instance, kwargs, ctx)
    return instance


def _collect_fields(
    model: type[models.Model],
    kwargs: dict[str, object],
    ctx: SeedContext,
) -> models.ForeignKey[Any] | None:
    """Fill kwargs for one model; return a non-null self-FK when one exists."""
    self_fk: models.ForeignKey[Any] | None = None
    for field in model._meta.get_fields():
        if not isinstance(field, models.Field):
            continue
        if field.auto_created or not field.concrete or not field.editable:
            continue
        if isinstance(field, models.ForeignKey):
            if field.remote_field.model is model:
                if not field.null:
                    self_fk = field  # seeded as a second pass, never at insert
                continue
            if field.null:
                # A nullable non-self FK stays empty: inventing a parent row can
                # violate the pair CHECKs that make the column nullable at all
                # (evidencerequest_kind_obligation_check is the standing example).
                continue
            kwargs[field.name] = _parent_for(field, ctx)
            continue
        if field.null or field.has_default():
            continue
        kwargs[field.name] = _default_for(field, ctx)
    return self_fk


def _seed_action_proposal(model: type[models.Model], ctx: SeedContext) -> models.Model:
    """ActionProposal whose manifest_bytes the frozen-proposal trigger accepts.

    The trigger compares manifest fields to the row's own columns, so the row is
    built first (minus manifest/digest) and the manifest mirrors it verbatim.
    """
    proposal_id = uuid6.uuid7()
    expires = timezone.now() + dt.timedelta(days=30)
    not_before = timezone.now() - dt.timedelta(minutes=1)
    cycle = seed_model(django_apps.get_model("cycles", "ServiceCycle"), ctx)
    intent = f"seed-{ctx.tag}-{proposal_id.hex[:12]}"
    manifest = {
        "schema": "axorim.effect.v1",
        "tenant_id": str(ctx.tenant.id),
        "client_id": str(ctx.client.id),
        "cycle_id": str(cycle.pk),
        "proposal_id": str(proposal_id),
        "proposal_revision": 1,
        "initiator_id": str(ctx.user.id),
        "business_intent_key": intent,
        "action_kind": ActionKind.EMAIL_NOTICE.value,
        "expires_at": expires.isoformat(),
        "not_before": not_before.isoformat(),
        "payload": {},
        "observations": [],
    }
    body = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        return _manager(model).create(
            id=proposal_id,
            tenant=ctx.tenant,
            client=ctx.client,
            cycle=cycle,
            initiator=ctx.user,
            revision=1,
            business_intent_key=intent,
            action_kind=ActionKind.EMAIL_NOTICE.value,
            input_spec={},
            expires_at=expires,
            not_before=not_before,
            manifest_bytes=body,
            digest=hashlib.sha256(body).hexdigest(),
        )


def _seed_client_publication(
    model: type[models.Model], ctx: SeedContext
) -> models.Model:
    """ClientPublication whose insert satisfies the frozen-publication trigger.

    The trigger reads the proposal's manifest and demands: action_kind
    'client_publication', the same cycle and business_intent_key, payload
    subject/body mirroring title/body_ptbr, payload documents identical to the
    row's documents column, and no 'withdrawal' key in payload.
    """
    cycle = seed_model(django_apps.get_model("cycles", "ServiceCycle"), ctx)
    intent = f"pub-{ctx.tag}-{uuid6.uuid7().hex[:12]}"
    documents: list[dict[str, str]] = [
        {"id": "d1", "sha256": "a" * 64, "name": "doc.pdf"}
    ]
    title = f"pub {ctx.tag}"
    body_ptbr = "corpo"
    expires = timezone.now() + dt.timedelta(days=30)
    not_before = timezone.now() - dt.timedelta(minutes=1)
    proposal_id = uuid6.uuid7()
    manifest = {
        "schema": "axorim.effect.v1",
        "tenant_id": str(ctx.tenant.id),
        "client_id": str(ctx.client.id),
        "cycle_id": str(cycle.pk),
        "proposal_id": str(proposal_id),
        "proposal_revision": 1,
        "initiator_id": str(ctx.user.id),
        "business_intent_key": intent,
        "action_kind": ActionKind.CLIENT_PUBLICATION.value,
        "expires_at": expires.isoformat(),
        "not_before": not_before.isoformat(),
        "payload": {
            "subject": title,
            "body": body_ptbr,
        },
        "documents": documents,
        "observations": [],
    }
    manifest_bytes = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True
    ).encode()
    proposal_model = django_apps.get_model("cycles", "ActionProposal")
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        proposal = _manager(proposal_model).create(
            id=proposal_id,
            tenant=ctx.tenant,
            client=ctx.client,
            cycle=cycle,
            initiator=ctx.user,
            revision=1,
            business_intent_key=intent,
            action_kind=ActionKind.CLIENT_PUBLICATION.value,
            input_spec={},
            expires_at=expires,
            not_before=not_before,
            manifest_bytes=manifest_bytes,
            digest=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        ctx.rows["cycles.actionproposal"] = proposal
        return _manager(model).create(
            tenant=ctx.tenant,
            client=ctx.client,
            proposal=proposal,
            proposal_revision=1,
            cycle=cycle,
            title=title,
            body_ptbr=body_ptbr,
            documents=documents,
            # publication_exact_proposal_fk pins the digest column to the
            # proposal's own digest — it is a twin, not a fresh hash.
            publication_digest=hashlib.sha256(manifest_bytes).hexdigest(),
            business_intent_key=intent,
        )


def _seed_bulk_operation(model: type[models.Model], ctx: SeedContext) -> models.Model:
    """BulkOperation whose snapshot_digest verifies under checked_items.

    load_operation recomputes the digest and refuses a mismatch before any
    scope check, so the stored snapshot must be built with the same canonical
    encoder the service uses.
    """
    from apps.cycles.bulk_contracts import SnapshotItem  # noqa: PLC0415
    from apps.cycles.bulk_contracts import digest as bulk_digest  # noqa: PLC0415

    snapshot: JSONValue = {
        "schema": "axorim.bulk.v1",
        "items": [
            SnapshotItem(
                subject_ref=ctx.client.pk,
                client_id=ctx.client.pk,
                label=f"item {ctx.tag}",
                expected_version=1,
                eligible=True,
                reason="",
                effect={},
            ).as_data()
        ],
    }
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        return _manager(model).create(
            tenant=ctx.tenant,
            kind=_first_choice(
                cast("models.Field[Any, Any]", model._meta.get_field("kind"))
            ),
            requested_by=ctx.user,
            snapshot_json=snapshot,
            snapshot_digest=bulk_digest(snapshot),
            valid_until=timezone.now() + dt.timedelta(days=1),
            idempotency_key=f"bulk-{ctx.tag}-{uuid6.uuid7().hex[:12]}",
        )


def _seed_evidence_link(model: type[models.Model], ctx: SeedContext) -> models.Model:
    """EvidenceLink whose DocumentIntake covers the SAME document row.

    document-preview scopes by link then reads the intake under
    link.document_id; an independently seeded intake on another document
    leaves the preview 404 even for the row's own tenant.
    """
    document = seed_model(django_apps.get_model("obligations", "Document"), ctx)
    request = seed_model(django_apps.get_model("cycles", "EvidenceRequest"), ctx)
    intake_model = django_apps.get_model("cycles", "DocumentIntake")
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        if not _manager(intake_model).filter(document=document).exists():
            _manager(intake_model).create(
                tenant=ctx.tenant,
                client=ctx.client,
                document=document,
                state="verificado",
            )
        return _manager(model).create(
            tenant=ctx.tenant,
            client=ctx.client,
            request=request,
            document=document,
            origin="portal",
            submitted_by=ctx.user,
        )


def _seed_billing_account(model: type[models.Model], ctx: SeedContext) -> models.Model:
    """BillingAccount with a renderable invoice date for the account page."""
    kwargs: dict[str, object] = {"tenant": ctx.tenant}
    _collect_fields(model, kwargs, ctx)
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        return _manager(model).create(**kwargs)


def _seed_purge_claim(model: type[models.Model], ctx: SeedContext) -> models.Model:
    """ContentPurgeClaim whose identity the purge trigger can verify.

    content_kind='note' points at the seeded InternalNote; its branch checks
    only the target row's existence and an empty storage descriptor, which the
    JSONField default already supplies.
    """
    note = seed_model(django_apps.get_model("cycles", "InternalNote"), ctx)
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        return _manager(model).create(
            tenant=ctx.tenant,
            client=ctx.client,
            content_kind="note",
            content_id=note.pk,
        )


def _link_self_parent(
    model: type[models.Model],
    field: models.ForeignKey[Any, Any],
    instance: models.Model,
    kwargs: dict[str, object],
    ctx: SeedContext,
) -> None:
    """Point a required self-FK at the row itself (supersedes/correction_of).

    A separate root row is deliberately NOT created: several of these tables
    carry range-exclusion or unique constraints on (client, period) that a
    duplicate would trip. A self-pointing link satisfies NOT NULL and the
    tenant-paired composite without inventing a sibling row.
    """
    with tenant_context(ctx.tenant.id), client_context(ctx.client.id):
        setattr(instance, field.attname, instance.pk)
        instance.save(update_fields=[field.attname])


def _parent_for(
    field: models.ForeignKey[Any, Any],
    ctx: SeedContext,
) -> object:
    """Resolve one FK inside the same tenant (and client pair when scoped)."""
    target = field.remote_field.model
    pinned: dict[type[models.Model], object] = {
        Tenant: ctx.tenant,
        ClientCompany: ctx.client,
        User: ctx.user,
    }
    if field.name == "tenant" or target in pinned:
        return pinned[target]
    if target is Membership:
        membership = ctx.rows.get("tenants.membership")
        if membership is None:
            # Reuse the roster row make_seed_context made: the
            # (user, tenant, client) unique admits exactly one.
            membership = Membership.objects.filter(
                user=ctx.user, tenant=ctx.tenant, client__isnull=True
            ).first()
            if membership is None:
                membership = Membership.objects.create(
                    user=ctx.user,
                    tenant=ctx.tenant,
                    role=TenantRole.STAFF_ACCOUNTANT,
                    client=None,
                )
            ctx.rows["tenants.membership"] = membership
        return membership
    if issubclass(target, TenantScopedModel):
        return seed_model(target, ctx)
    # Platform reference data seeded by migrations (ObligationType and friends).
    manager = _manager(target)
    existing = manager.first()
    if existing is not None:
        return existing
    return manager.create(name="seed", code="SEED")


def _sync_twin_fields(
    model: type[models.Model],
    kwargs: dict[str, object],
) -> None:
    """Copy the parent's repeated columns into the child's kwargs."""
    twins: tuple[tuple[str, str, dict[str, str]], ...] = (
        (
            "cycles.ActionExecution",
            "proposal",
            {
                "proposal_revision": "revision",
                "digest": "digest",
                # execution_known_kind + the proposal-pair composite want the
                # parent's own values, not a fresh default.
                "action_kind": "action_kind",
                "cycle": "cycle",
                "business_intent_key": "business_intent_key",
            },
        ),
        (
            "cycles.Approval",
            "proposal",
            {"proposal_revision": "revision", "digest": "digest"},
        ),
        (
            "cycles.ClientPublication",
            "proposal",
            {"proposal_revision": "revision", "publication_digest": "digest"},
        ),
        (
            "cycles.ClientAcknowledgement",
            "publication",
            {"publication_digest": "publication_digest"},
        ),
    )
    field_names = {f.name for f in model._meta.fields}
    for label, fk_name, mapping in twins:
        if model._meta.label != label or kwargs.get(fk_name) is None:
            continue
        parent: Any = kwargs[fk_name]
        for child_field, parent_field in mapping.items():
            if child_field in field_names:
                kwargs[child_field] = getattr(parent, parent_field)


def make_seed_context(slug: str, tag: str, *, cnpj: str | None = None) -> SeedContext:
    """One firm (all flags on), one user on its roster, one client."""
    tenant = Tenant.objects.create(
        name=f"Sweep {tag}",
        slug=slug,
        features=[
            "cycles_r1",
            "briefing_r1",
            "ai_shadow",
            "bulk_r1",
            "import_r1",
            "firm_settings_r1",
        ],
    )
    user = User.objects.create_user(email=f"actor@{slug}.test")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    with tenant_context(tenant.id):
        client = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name=f"CLIENTE {tag.upper()} MARKER",
            trade_name=tag.upper(),
            cnpj=cnpj or ("11222333000181" if tag.endswith("a") else "11444777000161"),
            is_mei=True,
        )
    return SeedContext(tenant=tenant, client=client, user=user, tag=tag)


def seed_graph(slug: str, tag: str) -> SeedContext:
    """Seed one row of every swept model inside one (tenant, client) pair."""
    ctx = make_seed_context(slug, tag)
    with tenant_context(ctx.tenant.id):
        for model in axorim_models():
            seed_model(model, ctx)
    return ctx


def paired_parent_fks() -> dict[tuple[str, str], str]:
    """{(table, fk_column): constraint} — composites that include tenant_id.

    Read from the live catalog rather than declared: a migration adding a paired
    column also adds the constraint, and the sweep asserts behaviour against the
    constraint that actually shipped.
    """
    pairs: dict[tuple[str, str], str] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE contype = 'f'
               AND connamespace = 'public'::regnamespace
               AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (tenant_id,%%'
            """
        )
        for table, name, definition in cursor.fetchall():
            inside = definition.split("(", 1)[1].split(")", 1)[0]
            for column in (c.strip().strip('"') for c in inside.split(",")):
                if column not in {"tenant_id", "client_id"}:
                    pairs[(str(table), column)] = str(name)
    return pairs


def paired_parent_targets() -> dict[tuple[str, str], tuple[str, str]]:
    """{(table, fk_column): (parent_table, parent_column)}.

    The probe needs the referenced MODEL and the remote column each local
    column binds to: a column like delivery_outboundmessage.proposal_id is a
    plain UUID column (no Django FK) whose parent exists only inside the
    constraint definition, and exact-FK twins (proposal_revision, digest) must
    not be probed as parents — they bind remote 'revision'/'digest', not 'id'.
    """
    targets: dict[tuple[str, str], tuple[str, str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT conrelid::regclass::text, pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE contype = 'f'
               AND connamespace = 'public'::regnamespace
               AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (tenant_id,%%'
            """
        )
        for table, definition in cursor.fetchall():
            local = [
                c.strip().strip('"')
                for c in definition.split("(", 1)[1].split(")", 1)[0].split(",")
            ]
            refs = definition.split("REFERENCES ", 1)[1]
            remote_table = refs.split("(", 1)[0].strip()
            remote_cols = [
                c.strip().strip('"')
                for c in refs.split("(", 1)[1].split(")", 1)[0].split(",")
            ]
            for local_col, remote_col in zip(local, remote_cols, strict=True):
                if local_col not in {"tenant_id", "client_id"}:
                    targets[(str(table), local_col)] = (
                        remote_table,
                        remote_col,
                    )
    return targets


def iterable_labels(models_: Iterable[type[models.Model]]) -> list[str]:
    return [m._meta.label for m in models_]


def signed_in_client(user: User, host: str) -> Client:
    """A Django test client carrying the session a password+TOTP sign-in writes.

    `force_login` alone never records the authentication method, so a view or
    middleware that asks whether a second factor was used reads the session as
    stale and redirects — the caller would exercise the redirect, not the route.
    The user must already hold a TOTP authenticator (`tests.support.enrol_totp`).
    """
    import time  # noqa: PLC0415

    from allauth.account.internal.flows.login import (  # noqa: PLC0415
        AUTHENTICATION_METHODS_SESSION_KEY,
    )
    from allauth.mfa.models import Authenticator  # noqa: PLC0415
    from django.conf import settings  # noqa: PLC0415
    from django.test import Client as DjangoClient  # noqa: PLC0415

    client = DjangoClient(SERVER_NAME=host)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    authenticator = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
    session = client.session
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": time.time(), "email": user.email},
        {
            "method": "mfa",
            "at": time.time(),
            "id": authenticator.pk,
            "type": "totp",
        },
    ]
    session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = str(session.session_key)
    return client
