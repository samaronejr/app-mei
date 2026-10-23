"""F4 — the Must-NOT-Have table of `.omo/plans/accounting-mei-saas.md:41-52`, asserted.

Criterion C10 of the audit asks whether the tree still respects the exclusions the plan
declared. Reading the table and eyeballing the repo answers that once; this module
answers it on every run, so the next commit that quietly adds a PSP client turns red
instead of turning up in a Phase-3 retrospective.

Rows 1 and 2 (client portal PWA, document vault) are SUPERSEDED — they were built
deliberately under `.omo/plans/phase-2a-portal-isolation.md` and
`phase-2b-portal-write.md` in 2026-07 — so criterion 10 is measured over the remaining
eight rows, which each get one test below. Two clearly-labelled EXTRAS follow: they are
not table rows, they pin the half of a superseded row that is still banned and a piece
of plan prose.

**Every scan excludes `__pycache__`.** That directory is git-ignored and is written
*during* the pytest run, so an unfiltered scan pins machine-specific `.pyc` paths and
produces phantom CI failures. The precedent is `tests/obligations/test_due_rules.py`
`:181-183`, including its vacuity control at `:184`.

Every pinned constant below was derived by running its own scan under its own filter.
An anchor that stops holding means the tree changed: investigate the change, do not
widen the anchor.
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib import admin
from django.db import models
from django.http import HttpRequest
from django.urls import get_resolver

from apps.audit.admin import AccessLogAdmin, EventAdmin, PlatformEventAdmin
from apps.audit.models import AccessLog, DataSubjectRequest, Event, PlatformEvent
from apps.fiscal.models import UF

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TEXT_SUFFIXES = frozenset({".py", ".html", ".js", ".json", ".css"})

# Row 4. `nfs[-_]?e` under `apps/**/*.py`: the registry names the municipal system it
# stores a fact about, and nothing else does. `apps/audit/models.py` and
# `apps/authz/matrix.py` must stay at zero — an invoice workflow would reach both.
NFSE_ALLOWED_FILES = frozenset(
    {
        "apps/core/rls.py",
        "apps/fiscal/capabilities.py",
        "apps/fiscal/migrations/0001_initial.py",
        "apps/fiscal/migrations/0002_seed_capitals.py",
        "apps/fiscal/models.py",
        # AXR1-013 (superseded restriction): the R1 plan (T13, S5.4 M5) commissions
        # READING a national NFS-e XML the client uploaded. No request workflow,
        # audit action or capability: the forbidden files below stay at zero.
        "apps/cycles/extraction/__init__.py",
        "apps/cycles/extraction/nfse_xml.py",
        # AXR1-024: synthetic XML generator only; no issuance or invoice workflow.
        "apps/automation/dataset.py",
        # AXRM-007 (superseded restriction, ADR-0008): a client provider link names
        # the RECEIVED-document collections it may read (`nfsen_recebidas`). Reading,
        # never issuing: the audit actions and the capability matrix stay at zero.
        "apps/integrations/models.py",
        "apps/integrations/migrations/0001_initial.py",
        # AXRM-008: the client's authorization to READ received NFS-e (capture
        # authority service `captura_nfse`, operation FC-NFS); no issuance anywhere.
        "apps/integrations/authority.py",
        "apps/integrations/migrations/0002_authority_records.py",
        "apps/integrations/workspace.py",
        # AXRM-009: the uncertain-call issue's service choices repeat AXRM-008's
        # capture services and name the A6 Focus READ operations (FC-NFS, FC-PDF).
        "apps/integrations/migrations/0003_call_protocol.py",
    },
)
NFSE_FORBIDDEN_FILES = ("apps/audit/models.py", "apps/authz/matrix.py")

# Row 8. `connector` under `apps/**/*.py`: two audit action names and prose promising
# Phase 3 a seam. No implementation.
#
# `_connector="OR"` is discarded before the file set is judged. It is not a connector
# reference at all — it is the keyword Django's own migration writer emits when it
# serialises a `Q(...)` with a non-default connector, so it appears in whichever
# migration most recently declared a multi-condition constraint. Matching it would pin
# this test to a set of generated files that grows with every unrelated schema change,
# and nothing that hides behind that literal could be a connector framework.
CONNECTOR_WRITER_ARTEFACT = '_connector="OR"'
CONNECTOR_ALLOWED_FILES = frozenset(
    {
        "apps/audit/migrations/0001_initial.py",
        "apps/audit/models.py",
        "apps/clients/migrations/0004_seed_checklist_templates.py",
        "apps/clients/models/onboarding.py",
        "apps/clients/services.py",
        "apps/fiscal/models.py",
    },
)

# AXRM-009/A7.4 commissions provider-effect recovery, not bank reconciliation.
# Register only its exact modules; OFX remains forbidden even in these files.
PROVIDER_RECOVERY_FILES = frozenset(
    {
        "apps/integrations/call_dispatch.py",
        "apps/integrations/call_models.py",
        "apps/integrations/management/commands/reconcile_provider_operations.py",
        "apps/integrations/migrations/0003_call_protocol.py",
        "apps/integrations/provider_replay.py",
    }
)

# Row 10. The single `esocial` occurrence is a COMMENT above `has_employee`, saying why
# the field is a boolean rather than a payroll model. Pinned by location so that a
# future *field* named `esocial_*` cannot slip in under the same allowance.
ESOCIAL_ALLOWED_LOCATION = "apps/clients/models/company.py:151"

# Row 10, AXR1-013 (superseded restriction): the R1 plan (T13, S5.4 M5) commissions a
# read-only NF-e XML extractor for client uploads. Issuing, transmitting or managing
# NF-e stays out of scope: only these two files may name it.
NFE_READ_ONLY_FILES = frozenset(
    {
        "apps/cycles/extraction/__init__.py",
        "apps/cycles/extraction/nfe_xml.py",
        # AXR1-024: synthetic XML generator only.
        "apps/automation/dataset.py",
        # AXRM-007 (superseded restriction, ADR-0008): the received-document
        # collection labels of a client provider link. Read-only capture; issuing,
        # transmitting or managing NF-e stays out of scope.
        "apps/integrations/models.py",
        "apps/integrations/migrations/0001_initial.py",
        # AXRM-008: the client's authorization to READ received NF-e (operation
        # FC-NFE, service label "Captura de NF-e recebidas"); no issuance anywhere.
        "apps/integrations/authority.py",
        "apps/integrations/migrations/0002_authority_records.py",
        # AXRM-009: the same choices name the A6 read operation FC-NFE.
        "apps/integrations/migrations/0003_call_protocol.py",
    },
)

# Row 3. AXR1-022 explicitly commissions firm owner invitations and opted-in summaries,
# housed inside tenants. No notifications app, push channel or mail-template subsystem.
FIRM_NOTIFICATION_MODULES = frozenset(
    {
        "apps/tenants/notification_models.py",
        "apps/tenants/notification_tasks.py",
        "apps/tenants/notifications.py",
    }
)

# Row 7. T-033 commissioned the client CSV export. AXR1-018 explicitly commissions
# per-cycle evidence bundles (.omo/plans/axorim-pivot-r1.md:1839), including their
# status and download routes. AXR1-020/S7.3 commissions the private per-item bulk
# result CSV. AXR1-022 adds owner-only account portability, not portfolio KPI
# reports. Portfolio Reports v1 and an audit UI remain excluded.
EXPORT_URL_NAMES = frozenset(
    {
        "clients-export-csv",
        "evidence-export-detail",
        "evidence-export-download",
        "bulk-report-csv",
        "account-export-request",
        "account-export-detail",
        "account-export-download",
    }
)

# Row 4. The fiscal app is a municipal registry, full stop. AXRM-006 adds the
# registry's seed-ownership ledger (which rows a data migration inserted), which is
# registry bookkeeping and not an invoice or NFS-e workflow.
FISCAL_MODEL_NAMES = frozenset(
    {"Municipality", "MunicipalityCapability", "MunicipalitySeedRow"},
)

# Row 10, AXR1-023: the plan's own todo-23 block commissions the owner-facing
# support-grant pages under `/configuracoes/suporte/` ("The owner approves in
# /configuracoes/suporte/"). Those four routes are an owner approval surface,
# not the excluded support *console* — a console is the staff-facing tenant
# switcher, which still must not exist. The exemption names the exact routes
# and nothing else: any other route whose name carries either spelling fails.
SUPPORT_GRANT_OWNER_ROUTES = frozenset(
    {
        "firm-suporte",
        "firm-suporte-approve",
        "firm-suporte-revoke",
        "firm-suporte-trilha",
    }
)


def _sources(roots: Sequence[str], suffixes: Iterable[str] = (".py",)) -> list[Path]:
    """Return files under `roots` with one of `suffixes`, `__pycache__` excluded."""
    wanted = frozenset(suffixes)
    return sorted(
        path
        for root in roots
        for path in (PROJECT_ROOT / root).rglob("*")
        if path.is_file() and path.suffix in wanted and "__pycache__" not in path.parts
    )


def _grep(sources: Sequence[Path], pattern: str) -> list[str]:
    """Return `path:line: text` for every case-insensitive match, for a loud failure."""
    expression = re.compile(pattern, re.IGNORECASE)
    return [
        f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if expression.search(line)
    ]


def _outside(hits: Iterable[str], allowed: Iterable[str]) -> list[str]:
    """Return the hits whose file is not in the derived allow-set."""
    permitted = frozenset(allowed)
    return sorted(hit for hit in hits if hit.split(":", 1)[0] not in permitted)


def _report(label: str, hits: Sequence[str]) -> str:
    """Format offenders one per line, because a bare `assert []` names nothing."""
    return f"{label} — {len(hits)} offending line(s):\n" + "\n".join(hits)


def test_row_3_no_notifications() -> None:
    """Row 3: "Notifications (email templates, push, reminder sweeps) | Phase 2"."""
    # Given the application packages and the project's own template tree
    sources = _sources(("apps", "config"))
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"
    templates = _sources(("templates",), TEXT_SUFFIXES)
    assert templates, "the template scan found nothing and would pass vacuously"

    # When the three shapes a notifications subsystem takes are looked for: an app, an
    # installed-app entry, and a project-owned mail template. allauth ships its own
    # templates from site-packages, which this scan does not reach and does not judge.
    modules = [
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "apps").rglob("notification*")
        if "__pycache__" not in path.parts
    ]
    installed = [
        app for app in settings.INSTALLED_APPS if "notification" in app.lower()
    ]
    mail_templates = [
        str(path.relative_to(PROJECT_ROOT))
        for path in templates
        if re.search(r"mail", path.name, re.IGNORECASE)
    ]

    # T22 supersedes only the exact three firm-notice modules above. The installed
    # app and mail-template prohibitions remain unchanged; another module still fails.
    assert set(modules) == FIRM_NOTIFICATION_MODULES, _report(
        "notifications module outside T22", modules
    )
    assert installed == [], _report("notifications app installed", installed)
    assert mail_templates == [], _report("project email template", mail_templates)


def test_row_4_no_invoice_or_nfse_workflow() -> None:
    """Row 4: "Invoice registry, NFS-e request workflow, artifact capture | Phase 3"."""
    # Given the fiscal app as Django itself sees it
    fiscal_models = {
        model.__name__ for model in django_apps.get_app_config("fiscal").get_models()
    }

    # When its model set is compared with the registry the plan commissioned
    # Then it matches exactly. `UF` is deliberately absent: it is a `TextChoices` enum
    # at `apps/fiscal/models.py:28`, not a table, and asserting it as a model would make
    # this test wrong in a way that reads as thorough.
    assert fiscal_models == set(FISCAL_MODEL_NAMES), (
        f"fiscal models drifted: {sorted(fiscal_models)}"
    )
    assert issubclass(UF, models.TextChoices), (
        "UF must stay an enum, not become a table"
    )

    # And when every application source is searched for the NFS-e spelling
    sources = _sources(("apps",))
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"
    hits = _grep(sources, r"nfs[-_]?e")
    assert hits, "the NFS-e scan matched nothing; the registry should still name it"

    # Then it appears only where a municipal *fact* is recorded — never in the audit
    # actions or the permission matrix, which is where a request workflow would land.
    strays = _outside(hits, NFSE_ALLOWED_FILES)
    assert strays == [], _report("NFS-e outside the registry", strays)
    matched_files = {hit.split(":", 1)[0] for hit in hits}
    for forbidden in NFSE_FORBIDDEN_FILES:
        assert forbidden not in matched_files, f"NFS-e reached {forbidden}"


def test_row_5_no_psp_integration() -> None:
    """Row 5: "Asaas / any PSP integration, Pix, boleto, subscriptions | Phase 3"."""
    # Given every application and settings source, migrations INCLUDED — a payment
    # column arrives in a migration before it arrives in a model
    sources = _sources(("apps", "config"))
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When they are searched for the payment vocabulary. `\bpix\b` is bounded so that an
    # unrelated identifier containing the letters cannot fire.
    hits = _grep(sources, r"asaas|boleto|\bpix\b")

    # Then there are none at all. Money movement is Phase 3, and the first import is the
    # cheapest moment to notice it.
    assert hits == [], _report("PSP / payment reference", hits)


def test_row_6_no_bank_import() -> None:
    """Row 6: "OFX/CSV bank import, transactions, reconciliation | Phase 4"."""
    # Given every application source
    sources = _sources(("apps",))
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When they are searched for the bank-import vocabulary
    bank_hits = _grep(sources, r"\bofx\b")
    hits = _grep(sources, r"reconcil")

    # Then bank import stays excluded. The regional plan's provider recovery is
    # registered by exact file, never by app-wide or banking-vocabulary exemption.
    assert bank_hits == [], _report("bank-import reference", bank_hits)
    strays = _outside(hits, PROVIDER_RECOVERY_FILES)
    assert strays == [], _report("reconciliation outside provider recovery", strays)
    assert {hit.split(":", 1)[0] for hit in hits} == PROVIDER_RECOVERY_FILES


def test_row_7_no_reports_v1_and_no_audit_ui() -> None:
    """Row 7: "Reports v1, portfolio KPI exports, audit **UI** | Phase 3"."""
    # Given the three audit-stream admins, instantiated against the real site
    request = HttpRequest()
    read_only = [
        EventAdmin(Event, admin.site),
        PlatformEventAdmin(PlatformEvent, admin.site),
        AccessLogAdmin(AccessLog, admin.site),
    ]

    # When each is asked whether it permits writing
    # Then every answer is no. A changelist that refuses add, change and delete is a
    # window onto an append-only table, not "audit UI v1" — the excluded thing is an
    # interface for *working* the audit trail. `DataSubjectRequestAdmin` is writable and
    # is EXEMPT by design: closing a rights request is how the statutory LGPD workflow
    # is answered, which its own docstring at `apps/audit/admin.py:105` states.
    for model_admin in read_only:
        name = type(model_admin).__name__
        assert not model_admin.has_add_permission(request), f"{name} allows add"
        assert not model_admin.has_change_permission(request), f"{name} allows change"
        assert not model_admin.has_delete_permission(request), f"{name} allows delete"
    assert admin.site.is_registered(DataSubjectRequest), (
        "the exempt statutory workflow disappeared; re-adjudicate before relaxing this"
    )

    # And when the URLconf is enumerated — the resolver, not a grep, because only a
    # routed name is a reachable report
    names = sorted(
        name for name in get_resolver().reverse_dict if isinstance(name, str)
    )
    assert len(names) > 10, (
        "the URLconf enumeration found nothing and would pass vacuously"
    )
    reporting = {
        name for name in names if re.search(r"report|kpi|export", name, re.IGNORECASE)
    }

    # Then only the explicitly commissioned CSV exports are reachable.
    assert reporting == set(EXPORT_URL_NAMES), _report(
        "reporting route beyond the commissioned CSV exports",
        sorted(reporting - set(EXPORT_URL_NAMES)),
    )


def test_row_8_no_connector_framework() -> None:
    """Row 8: "Connector framework implementations (native/provider/portal-assist)"."""
    # Given every application source
    sources = _sources(("apps",))
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When a connectors package is looked for, and the word is searched for
    packages = [
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "apps").rglob("connector*")
        if "__pycache__" not in path.parts
    ]
    hits = [
        hit
        for hit in _grep(sources, r"connector")
        if CONNECTOR_WRITER_ARTEFACT not in hit
    ]
    assert hits, "the connector scan matched nothing; the audit action should name it"

    # Then no package exists, and the word occurs only in the derived allow-set: two
    # audit action names and prose promising Phase 3 a seam, never an implementation.
    assert packages == [], _report("connector package", packages)
    strays = _outside(hits, CONNECTOR_ALLOWED_FILES)
    assert strays == [], _report("connector reference in an unpinned file", strays)


def test_row_9_no_govbr_oidc_login() -> None:
    """Row 9: "gov.br OIDC login | Post-pilot"."""
    # Given the authentication configuration as Django resolved it
    backends = [backend.lower() for backend in settings.AUTHENTICATION_BACKENDS]
    providers = getattr(settings, "SOCIALACCOUNT_PROVIDERS", {})
    social_apps = [
        app for app in settings.INSTALLED_APPS if "socialaccount" in app.lower()
    ]

    # When it is inspected for a federated-identity path
    offenders = [
        backend for backend in backends if "oidc" in backend or "openid" in backend
    ]

    # Then there is none. The gov.br *trust level* recorded on a client company
    # (`apps/clients/models/company.py:53`) is deliberately data a human types —
    # recording a tier is not logging in with it, and only the latter is excluded here.
    assert offenders == [], _report("OIDC authentication backend", offenders)
    assert social_apps == [], _report("allauth socialaccount installed", social_apps)
    assert providers == {}, _report("configured social provider", sorted(providers))


def test_row_10_no_esocial_nfe_whatsapp_or_support_console() -> None:
    """Row 10: "eSocial payroll, NF-e/NFC-e, WhatsApp, support console | Out of MVP"."""
    # Given every application source
    sources = _sources(("apps",))
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When each excluded surface is searched for separately. `\bnf-?e\b` is bounded so
    # that it cannot match `nfse` — the municipal registry of row 4 is a different thing
    # from the state-level electronic invoice, and conflating them would make this test
    # fail on legitimate code.
    whatsapp = _grep(sources, r"whatsapp")
    esocial = _grep(sources, r"esocial")
    nfe = _outside(_grep(sources, r"\bnf-?e\b|\bnfc-?e\b"), NFE_READ_ONLY_FILES)
    console = sorted(
        name
        for name in get_resolver().reverse_dict
        if isinstance(name, str)
        and re.search(r"support|suporte", name, re.IGNORECASE)
        and name not in SUPPORT_GRANT_OWNER_ROUTES
    )

    # Then WhatsApp, NF-e/NFC-e and support-console routes are absent outright, and the
    # single eSocial occurrence is the COMMENT above `has_employee` explaining why that
    # field is a boolean. Pinned by location: a *field* named `esocial_*` would land on
    # a different line and fail here, which is the point.
    assert whatsapp == [], _report("WhatsApp reference", whatsapp)
    assert nfe == [], _report("NF-e / NFC-e reference", nfe)
    assert console == [], _report("support-console route", console)
    locations = {":".join(hit.split(":", 2)[:2]) for hit in esocial}
    assert locations == {ESOCIAL_ALLOWED_LOCATION}, _report(
        f"eSocial outside the pinned comment at {ESOCIAL_ALLOWED_LOCATION}",
        esocial,
    )


def test_extra_no_pwa_shell() -> None:
    """EXTRA — not a table row: the PWA half of superseded row 1 stays banned.

    Row 1 was superseded because the client *portal* shipped under phase-2a/2b. The
    portal is server-rendered. The offline shell the row also named — service worker,
    web manifest, web push — was not built and is not in scope, so the supersession
    must not be read as licence for it.
    """
    # Given every source, template and static asset the project owns
    sources = _sources(("apps", "static", "templates"), TEXT_SUFFIXES)
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When they are searched for the installable-app vocabulary
    hits = _grep(
        sources, r"service-worker|serviceworker|manifest\.json|webpush|web-push"
    )

    # Then there is none.
    assert hits == [], _report("PWA shell reference", hits)


def test_extra_no_clinic_project_fork() -> None:
    """EXTRA — not a table row: the plan's prose at `:17` and `:54`.

    "It also does **not** fork `clinic_project`" — that repo is reference-only, so a
    copied file naming its package is the observable failure.
    """
    # Given the CODE roots only. Scanning the repository root would read
    # `.omo/plans/accounting-mei-saas.md`, whose own prose contains the string, and this
    # test would fail on the document that commissions it.
    sources = _sources(("apps", "config", "static", "templates"), TEXT_SUFFIXES)
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When they are searched for the reference repository's package name
    hits = _grep(sources, r"clinic_project")

    # Then nothing was copied across.
    assert hits == [], _report("clinic_project reference in shipped code", hits)
