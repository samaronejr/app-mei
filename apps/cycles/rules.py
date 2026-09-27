"""Parsing and validation for what a playbook draft asserts: steps and targets.

Two grammars live here, both deliberately small:

* `due_rule` — when a generated task falls due. `period_end_offset` places it a
  number of days after the covered month closes; `fixed_day` names a calendar
  day of the month that follows; `obligation_due` borrows the resolved due date
  of a statutory obligation type, so a DAS-shaped deadline tracks the holiday
  table instead of being re-derived. Anything outside these three is refused:
  an unreadable rule that silently passed would generate deadlines nobody chose.
* `applies_to` — which clients a published version targets. The value sets are
  the literals T5's `TaxProfile.regime` and `Engagement.service_kind` enumerate;
  they live as local tuples because those models land on a sibling branch, and
  the plan requires the literal spellings, not a model dependency.

`StepSpec` is the boundary between the editor's form fields and the row the
service writes: validation happens here, on parsed data, never on POST strings.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Final

from apps.cycles.errors import DagCycleError, PlaybookValidationError
from apps.cycles.models import OwnerRule, StepKind

KEY_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# ObligationType codes are uppercase (DAS, DASN) — a different alphabet from a
# step key, deliberately, so the two namespaces can never collide.
OBLIGATION_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9-]{0,15}$")

# The regimes `clients_taxprofile.regime` enumerates (T5), spelled out so the
# JSON contract is stable whether or not that model is installed yet.
REGIMES: Final[tuple[str, ...]] = (
    "mei",
    "simples",
    "presumido",
    "real",
    "isento",
    "unknown",
)

# The service kinds `clients_engagement.service_kind` enumerates (T5).
SERVICE_KINDS: Final[tuple[str, ...]] = (
    "mei_mensal",
    "documentos_mensal",
    "outro",
)

APPLIES_TO_KEYS: Final[frozenset[str]] = frozenset({"regimes", "service_kinds"})


class DueRuleKind(StrEnum):
    """The three places a due date can come from — the whole list, no others."""

    PERIOD_END_OFFSET = "period_end_offset"
    FIXED_DAY = "fixed_day"
    OBLIGATION_DUE = "obligation_due"


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One step as the editor submits it — parsed, not yet a row."""

    key: str
    kind: str
    label_ptbr: str
    client_label_ptbr: str = ""
    depends_on: tuple[str, ...] = ()
    owner_rule: str = "team"
    due_rule: dict[str, object] = field(default_factory=dict)
    evidence_required: bool = False
    client_visible: bool = False
    is_enabled: bool = True


def _int_field(raw: dict[str, object], name: str, *, low: int, high: int) -> int:
    """Read one bounded integer out of a rule dict, refusing every other shape."""
    value = raw.get(name)
    # bool is an int subclass; a JSON true here is a shape error, not a value.
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"due_rule {name} must be an integer, got {value!r}"
        raise PlaybookValidationError([msg])
    if not low <= value <= high:
        msg = f"due_rule {name} must be between {low} and {high}, got {value}"
        raise PlaybookValidationError([msg])
    return value


def parse_due_rule(raw: object, *, step_key: str) -> dict[str, object]:
    """Validate one step's `due_rule`, returning the dict unchanged if honourable.

    Follows `apps.obligations.rules.parse_due_rule`: refuse rather than default.
    An unknown `kind` that became "no deadline" would generate tasks that never
    fall due and read as a product bug rather than the authoring error it is.
    """
    if not isinstance(raw, dict):
        msg = f"etapa {step_key}: due_rule deve ser um objeto JSON."
        raise PlaybookValidationError([msg])
    unknown = set(raw) - {"kind", "days", "day", "month_offset", "obligation"}
    if unknown:
        msg = f"etapa {step_key}: due_rule tem chaves desconhecidas {sorted(unknown)}."
        raise PlaybookValidationError([msg])
    kind = raw.get("kind")
    try:
        rule_kind = DueRuleKind(str(kind))
    except ValueError as exc:
        permitted = ", ".join(member.value for member in DueRuleKind)
        msg = f"etapa {step_key}: due_rule.kind {kind!r} não é um de {permitted}."
        raise PlaybookValidationError([msg]) from exc

    if rule_kind is DueRuleKind.PERIOD_END_OFFSET:
        days = _int_field(raw, "days", low=0, high=62)
        return {"kind": rule_kind.value, "days": days}
    if rule_kind is DueRuleKind.FIXED_DAY:
        day = _int_field(raw, "day", low=1, high=28)
        # Capped at 28 on purpose: February exists, and a rule that silently
        # cannot fire in some months is worse than one that cannot express the
        # 31st — that deadline is expressed as a period-end offset instead.
        month_offset = _int_field(raw, "month_offset", low=0, high=3)
        return {
            "kind": rule_kind.value,
            "day": day,
            "month_offset": month_offset,
        }
    obligation = raw.get("obligation")
    if not isinstance(obligation, str) or not OBLIGATION_PATTERN.match(obligation):
        msg = (
            f"etapa {step_key}: due_rule.obligation deve ser o código de um "
            f"tipo de obrigação, recebido {obligation!r}."
        )
        raise PlaybookValidationError([msg])
    return {"kind": rule_kind.value, "obligation": obligation}


def validate_applies_to(raw: object) -> dict[str, list[str]]:
    """Validate one version's `applies_to`, returning the normalized dict.

    Empty or absent lists mean unrestricted on that axis — a pack with no
    `regimes` targets every regime — never "matches nothing", which would
    quietly publish a version that generates for nobody.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = "applies_to deve ser um objeto JSON com regimes e service_kinds."
        raise PlaybookValidationError([msg])
    unknown = set(raw) - APPLIES_TO_KEYS
    if unknown:
        msg = f"applies_to tem chaves desconhecidas {sorted(unknown)}."
        raise PlaybookValidationError([msg])
    normalized: dict[str, list[str]] = {}
    for key, permitted in (
        ("regimes", REGIMES),
        ("service_kinds", SERVICE_KINDS),
    ):
        values = raw.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            msg = f"applies_to.{key} deve ser uma lista de valores."
            raise PlaybookValidationError([msg])
        rejected = sorted(set(values) - set(permitted))
        if rejected:
            msg = (
                f"applies_to.{key} contém valores que não existem: "
                f"{', '.join(rejected)}."
            )
            raise PlaybookValidationError([msg])
        normalized[key] = sorted(set(values))
    return normalized


def _validate_step(
    spec: StepSpec,
    *,
    keys: set[str],
    step_kinds: set[str],
    owner_rules: set[str],
) -> list[str]:
    """Collect every reason one spec cannot become a row — all of them, not one."""
    errors: list[str] = []
    if not KEY_PATTERN.match(spec.key):
        errors.append(
            f"etapa {spec.key!r}: a chave deve ser minúscula, com letras, "
            "números e hífens."
        )
    if spec.key in keys:
        errors.append(f"etapa {spec.key!r}: chave duplicada.")
    if spec.kind not in step_kinds:
        errors.append(f"etapa {spec.key!r}: tipo {spec.kind!r} não existe.")
    if not spec.label_ptbr.strip():
        errors.append(f"etapa {spec.key!r}: o rótulo é obrigatório.")
    if spec.client_visible and not spec.client_label_ptbr.strip():
        errors.append(
            f"etapa {spec.key!r}: o rótulo para o cliente é obrigatório quando "
            "a etapa é visível no portal."
        )
    if spec.owner_rule not in owner_rules:
        errors.append(
            f"etapa {spec.key!r}: regra de responsável {spec.owner_rule!r} não existe."
        )
    if not isinstance(spec.depends_on, tuple) or not all(
        isinstance(key, str) for key in spec.depends_on
    ):
        errors.append(f"etapa {spec.key!r}: dependências devem ser chaves.")
    elif spec.key in spec.depends_on:
        errors.append(f"etapa {spec.key!r}: uma etapa não depende de si mesma.")
    try:
        parse_due_rule(spec.due_rule, step_key=spec.key)
    except PlaybookValidationError as exc:
        errors.extend(exc.errors)
    return errors


def _find_cycle(edges: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Return the keys of one dependency cycle, or () when the graph is a DAG.

    Iterative DFS over `edges`; the colour map marks permanently-finished nodes
    black and on-stack ones grey, so a back-edge found mid-walk is the cycle.
    """
    grey, black = 1, 2
    colour: dict[str, int] = {}
    for root in edges:
        if colour.get(root) == black:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        colour[root] = grey
        path.append(root)
        while stack:
            node, index = stack[-1]
            neighbours = edges.get(node, ())
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                neighbour = neighbours[index]
                if colour.get(neighbour) == grey:
                    start = path.index(neighbour)
                    return (*path[start:], neighbour)
                if colour.get(neighbour) is None:
                    colour[neighbour] = grey
                    path.append(neighbour)
                    stack.append((neighbour, 0))
            else:
                colour[node] = black
                path.pop()
                stack.pop()
    return ()


def validate_draft(
    *,
    applies_to: object,
    steps: list[StepSpec],
    known_obligations: set[str],
    for_publish: bool = True,
) -> dict[str, list[str]]:
    """Validate a whole draft before it is saved or published.

    Returns the normalized `applies_to` on success and raises `PlaybookValidationError`
    — `DagCycleError` when the dependency graph loops — carrying every problem
    found, because a banner that names one error at a time makes an author
    resubmit once per fix.

    `for_publish=False` is the *save* path: an empty draft may be stored, and a
    cyclic one may be stored too — the publish contract (cycle → refusal) lives
    in the transition, and the editor needs to be able to save a half-ordered
    list it is still working on. Everything else — malformed keys, unknown
    dependency names, unreadable due rules — is refused at save as well.
    """
    normalized = validate_applies_to(applies_to)
    seen: set[str] = set()
    specs: dict[str, StepSpec] = {}
    kinds = {member.value for member in StepKind}
    owner_rules = {member.value for member in OwnerRule}
    errors: list[str] = []
    for spec in steps:
        errors.extend(
            _validate_step(
                spec,
                keys=seen,
                step_kinds=kinds,
                owner_rules=owner_rules,
            )
        )
        seen.add(spec.key)
        specs.setdefault(spec.key, spec)

    errors.extend(_dependency_errors(specs))

    if for_publish and not errors:
        cycle = _find_cycle({key: spec.depends_on for key, spec in specs.items()})
        if cycle:
            raise DagCycleError(cycle)

    errors.extend(_obligation_errors(specs, known_obligations))

    if errors:
        raise PlaybookValidationError(errors)
    if for_publish and not steps:
        msg = "Uma versão publicável precisa de ao menos uma etapa."
        raise PlaybookValidationError([msg])
    return normalized


def _dependency_errors(specs: dict[str, StepSpec]) -> list[str]:
    """Name every `depends_on` that points at no step of this version."""
    errors: list[str] = []
    for key, spec in specs.items():
        errors.extend(
            f"etapa {key}: depende de {dependency!r}, que não existe nesta versão."
            for dependency in spec.depends_on
            if dependency not in specs
        )
    return errors


def _obligation_errors(
    specs: dict[str, StepSpec],
    known_obligations: set[str],
) -> list[str]:
    """Name every `obligation_due` rule that cites a code the calendar lacks."""
    errors: list[str] = []
    for key, spec in specs.items():
        if not isinstance(spec.due_rule, dict):
            # Already reported by _validate_step; the .get calls below would
            # turn that shape error into an AttributeError.
            continue
        obligation = spec.due_rule.get("obligation")
        if spec.due_rule.get("kind") == DueRuleKind.OBLIGATION_DUE and (
            obligation is not None and str(obligation) not in known_obligations
        ):
            errors.append(
                f"etapa {key}: a obrigação {obligation!r} não existe no calendário."
            )
    return errors


MONTHS_IN_YEAR: Final = 12


def next_period_start(today: date) -> date:
    """Return the first day of the month after the one `today` is in."""
    month = today.month + 1
    year = today.year
    if month > MONTHS_IN_YEAR:
        month = 1
        year += 1
    return date(year, month, 1)
