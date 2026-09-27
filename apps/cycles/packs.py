"""The system packs: recurring-work playbooks the platform ships to every firm.

The packs live in code, not in a fixture: they are versioned content the plan
specifies step by step (S5.4 M3), and an installer rather than seed data keeps
them reviewable, diffable and idempotent. `install_system_packs` creates them
as tenant-scoped `is_system` rows — never a shared null-tenant row two firms
could read through each other's tenancy — and is safe to call repeatedly:
existing published versions are left alone, a missing pack is created, and a
pack whose definition changed gains a new published version.

Labels are the product's pt-BR (S5.7): the `client_label_ptbr` of a step is the
sentence the client portal shows, so it says what is needed and why rather than
what the firm calls the task internally.
"""

from dataclasses import dataclass
from typing import Final

from django.db import transaction

from apps.core.flags import tenant_has
from apps.core.tenancy import tenant_context
from apps.cycles.models import Playbook, PlaybookVersion, VersionState
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from apps.tenants.models import Tenant

FLAG: Final = "cycles_r1"

OWNER_RESPONSIBLE: Final = "engagement_responsible"
OWNER_ASSIGNEE: Final = "primary_assignee"
OWNER_TEAM: Final = "team"


@dataclass(frozen=True, slots=True)
class SystemPack:
    """One pack as code: the identity, the targeting scope and the steps."""

    key: str
    name: str
    applies_to: dict[str, list[str]]
    steps: tuple[StepSpec, ...]


def _step(  # noqa: PLR0913 — a StepSpec's fields are the signature
    key: str,
    kind: str,
    label: str,
    *,
    client_label: str = "",
    depends_on: tuple[str, ...] = (),
    owner_rule: str = OWNER_TEAM,
    due_rule: dict[str, object] | None = None,
    evidence_required: bool = False,
    client_visible: bool = False,
    is_enabled: bool = True,
) -> StepSpec:
    return StepSpec(
        key=key,
        kind=kind,
        label_ptbr=label,
        client_label_ptbr=client_label,
        depends_on=depends_on,
        owner_rule=owner_rule,
        due_rule=due_rule or {},
        evidence_required=evidence_required,
        client_visible=client_visible,
        is_enabled=is_enabled,
    )


def _request(  # noqa: PLR0913 — every flag is a real choice on a request step
    key: str,
    label: str,
    *,
    client_label: str,
    due_day: int,
    required: bool,
    enabled: bool = True,
    owner_rule: str = OWNER_TEAM,
) -> StepSpec:
    """Build a request step the client answers early in the month."""
    return _step(
        key,
        "evidence_request",
        label,
        client_label=client_label,
        owner_rule=owner_rule,
        due_rule={"kind": "period_end_offset", "days": due_day},
        evidence_required=required,
        client_visible=True,
        is_enabled=enabled,
    )


# "MEI mensal" — the R1 anchor pack (S5.4 M3, the plan's todo-8 step list).
# Documents arrive in the first days of the month that follows the competence;
# the review and the approval gate sit before the guide goes out; the DAS due
# rule borrows the statutory date so the checkpoint lands on the real deadline
# rather than a fixed day that ignores holidays.
MEI_MENSAL: Final = SystemPack(
    key="mei-mensal",
    name="MEI mensal",
    applies_to={"regimes": ["mei"], "service_kinds": ["mei_mensal"]},
    steps=(
        _request(
            "notas-fiscais",
            "Notas fiscais emitidas no mês",
            client_label=("Envie as notas fiscais emitidas pela empresa no mês."),
            due_day=5,
            required=False,
        ),
        _request(
            "extrato-receitas",
            "Extrato ou comprovantes de receitas",
            client_label=(
                "Envie o extrato bancário ou os comprovantes das receitas do mês."
            ),
            due_day=5,
            required=True,
        ),
        _step(
            "conferir-faturamento",
            "task",
            "Conferir faturamento e limite MEI",
            depends_on=("notas-fiscais", "extrato-receitas"),
            owner_rule=OWNER_ASSIGNEE,
            due_rule={"kind": "fixed_day", "day": 10, "month_offset": 1},
            evidence_required=False,
        ),
        _step(
            "revisao-mensal",
            "review",
            "Revisão do mês",
            depends_on=("conferir-faturamento",),
            owner_rule=OWNER_ASSIGNEE,
            due_rule={"kind": "fixed_day", "day": 15, "month_offset": 1},
            evidence_required=True,
        ),
        _step(
            "aprovacao-envio",
            "approval_gate",
            "Aprovação do envio ao cliente",
            depends_on=("revisao-mensal",),
            owner_rule=OWNER_RESPONSIBLE,
            due_rule={"kind": "fixed_day", "day": 18, "month_offset": 1},
        ),
        _step(
            "publicar-guia-das",
            "client_publication",
            "Guia DAS do mês",
            client_label="A guia DAS do mês está disponível.",
            depends_on=("aprovacao-envio",),
            owner_rule=OWNER_ASSIGNEE,
            due_rule={"kind": "obligation_due", "obligation": "DAS"},
            client_visible=True,
        ),
        _step(
            "aviso-email",
            "email_notice",
            "Aviso por e-mail",
            depends_on=("publicar-guia-das",),
            owner_rule=OWNER_TEAM,
            due_rule={"kind": "obligation_due", "obligation": "DAS"},
        ),
        _step(
            "pagamento-informado",
            "checkpoint",
            "Pagamento informado",
            client_label="Pagamento informado (não confirmado na Receita).",
            depends_on=("aviso-email",),
            owner_rule=OWNER_ASSIGNEE,
            due_rule={"kind": "fixed_day", "day": 22, "month_offset": 1},
            client_visible=True,
        ),
    ),
)

# "Coleta mensal" — the regime-neutral document-collection pack (S5.4 M3).
# "documentos-folha" ships disabled on purpose: payroll documents carry employee
# personal data, so the item exists but stays off until the firm turns it on
# deliberately (OG-08). Because a dependency must name an enabled-resolvable
# step, the review depends only on the three items that are on by default.
COLETA_MENSAL: Final = SystemPack(
    key="coleta-mensal",
    name="Coleta mensal",
    applies_to={"service_kinds": ["documentos_mensal"]},
    steps=(
        _request(
            "extratos-bancarios",
            "Extratos bancários",
            client_label="Envie os extratos bancários do mês da empresa.",
            due_day=5,
            required=True,
        ),
        _request(
            "notas-entrada",
            "Notas de entrada",
            client_label="Envie as notas fiscais de entrada do mês.",
            due_day=5,
            required=True,
        ),
        _request(
            "notas-saida",
            "Notas de saída",
            client_label="Envie as notas fiscais de saída emitidas no mês.",
            due_day=5,
            required=True,
        ),
        _request(
            "documentos-folha",
            "Documentos de folha",
            client_label="Envie os documentos de folha de pagamento do mês.",
            due_day=5,
            required=False,
            enabled=False,
        ),
        _step(
            "revisao-mensal",
            "review",
            "Revisão do mês",
            depends_on=("extratos-bancarios", "notas-entrada", "notas-saida"),
            owner_rule=OWNER_ASSIGNEE,
            due_rule={"kind": "fixed_day", "day": 15, "month_offset": 1},
            evidence_required=True,
        ),
        _step(
            "publicar-resumo",
            "client_publication",
            "Resumo do mês",
            client_label="O resumo do mês está disponível.",
            depends_on=("revisao-mensal",),
            owner_rule=OWNER_ASSIGNEE,
            due_rule={"kind": "fixed_day", "day": 20, "month_offset": 1},
            client_visible=True,
        ),
        _step(
            "aviso-email",
            "email_notice",
            "Aviso por e-mail",
            depends_on=("publicar-resumo",),
            owner_rule=OWNER_TEAM,
            due_rule={"kind": "fixed_day", "day": 20, "month_offset": 1},
        ),
    ),
)

SYSTEM_PACKS: Final[tuple[SystemPack, ...]] = (MEI_MENSAL, COLETA_MENSAL)


def _fingerprint(pack: SystemPack) -> list[tuple[str, str, bool]]:
    """Reduce a pack to what a re-sync must compare: key, kind, enabled."""
    return [(s.key, s.kind, s.is_enabled) for s in pack.steps]


def _version_matches(version: PlaybookVersion, pack: SystemPack) -> bool:
    """Report whether an installed version still equals the pack's definition."""
    stored = [
        (step.key, step.kind, step.is_enabled)
        for step in version.steps.order_by("position")
    ]
    return stored == _fingerprint(pack)


def install_system_packs(tenant: Tenant) -> list[Playbook]:
    """Ensure the system packs exist for `tenant` — idempotent, flag-gated.

    Called when `cycles_r1` lands on a tenant (the `Tenant` post_save hook), and
    safe to re-run: existing rows are compared by content fingerprint and only a
    definition that drifted gains a new published version. Nothing here holds a
    null tenant or bypasses RLS — every row is written inside `tenant_context`,
    so the transaction this may join (the admin save that granted the flag)
    commits the flag and the packs together or not at all.

    `transaction.atomic` wraps the whole pass: a crash between two packs must
    not leave the tenant half-installed and reported ready.
    """
    if not tenant_has(tenant.pk, FLAG):
        return []
    installed: list[Playbook] = []
    with tenant_context(tenant.pk), transaction.atomic():
        for pack in SYSTEM_PACKS:
            playbook = Playbook.objects.select_for_update().filter(key=pack.key).first()
            if playbook is not None and not playbook.is_system:
                # A tenant playbook already owns this key: the firm's own row
                # wins, and the pack is never written over it.
                continue
            if playbook is None:
                playbook = Playbook.objects.create(
                    tenant=tenant,
                    key=pack.key,
                    name=pack.name,
                    is_system=True,
                )
            latest = playbook.versions.order_by("-number").first()
            if (
                latest is not None
                and latest.state == VersionState.PUBLISHED
                and _version_matches(latest, pack)
            ):
                installed.append(playbook)
                continue
            if latest is None or latest.state != VersionState.DRAFT:
                latest = playbooks.create_draft_version(
                    actor=None,
                    tenant=tenant,
                    playbook_pk=playbook.pk,
                )
            draft_steps = list(pack.steps)
            playbooks.update_draft(
                actor=None,
                tenant=tenant,
                playbook_pk=playbook.pk,
                number=latest.number,
                expected_version=latest.version,
                steps=draft_steps,
                applies_to=pack.applies_to,
            )
            # Re-read the counter update_draft bumped, then publish. `actor=None`
            # is the installer's identity: publish still validates the DAG and
            # the due rules, so a malformed pack definition fails loudly here.
            refreshed = playbook.versions.get(number=latest.number)
            playbooks.publish_version(
                actor=None,
                tenant=tenant,
                playbook_pk=playbook.pk,
                number=latest.number,
                expected_version=refreshed.version,
            )
            installed.append(playbook)
    return installed


__all__ = [
    "COLETA_MENSAL",
    "FLAG",
    "MEI_MENSAL",
    "SYSTEM_PACKS",
    "SystemPack",
    "install_system_packs",
]
