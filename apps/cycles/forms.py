"""The playbook editor's forms: a typed field list, not a canvas.

Every step is a `StepForm` row inside one `formset`; ordering happens through
dedicated move posts, so the form itself never carries a position field — what a
user re-submits is content, not layout. `due_rule` is decomposed into typed
inputs (kind, day, month offset, obligation code) and re-assembled into the
`due_rule_json` shape `rules.parse_due_rule` validates, so the stored JSON is
never a raw string a user typed.
"""

from typing import Any

from django import forms
from django.forms import formset_factory
from django.utils.translation import gettext_lazy as _

from apps.cycles.errors import PlaybookValidationError
from apps.cycles.models import OwnerRule, PlaybookStep, StepKind
from apps.cycles.rules import (
    REGIMES,
    SERVICE_KINDS,
    DueRuleKind,
    StepSpec,
    parse_due_rule,
)

_REGIME_LABELS: dict[str, str] = {
    "mei": "MEI",
    "simples": "Simples Nacional",
    "presumido": "Lucro Presumido",
    "real": "Lucro Real",
    "isento": "Isento",
    "unknown": "Desconhecido",
}
_KIND_LABELS: dict[str, str] = {
    "mei_mensal": "MEI mensal",
    "documentos_mensal": "Documentos mensal",
    "outro": "Outro",
}
_DUE_LABELS: dict[str, str] = {
    DueRuleKind.PERIOD_END_OFFSET: "Dias após o fim do mês",
    DueRuleKind.FIXED_DAY: "Dia fixo do mês seguinte",
    DueRuleKind.OBLIGATION_DUE: "Vencimento de uma obrigação",
}

REGIME_CHOICES = [(value, _REGIME_LABELS[value]) for value in REGIMES]
SERVICE_KIND_CHOICES = [(value, _KIND_LABELS[value]) for value in SERVICE_KINDS]
DUE_KIND_CHOICES = [(value.value, _DUE_LABELS[value]) for value in DueRuleKind]


class PlaybookForm(forms.Form):
    """The identity fields of a playbook, and of the version being edited."""

    name = forms.CharField(
        label=_("Nome do playbook"),
        max_length=120,
    )
    applies_regimes = forms.MultipleChoiceField(
        label=_("Regimes"),
        choices=REGIME_CHOICES,
        required=False,
        help_text=_("Vazio significa todos os regimes."),
    )
    applies_service_kinds = forms.MultipleChoiceField(
        label=_("Serviços"),
        choices=SERVICE_KIND_CHOICES,
        required=False,
        help_text=_("Vazio significa todos os serviços."),
    )

    def applies_to(self) -> dict[str, list[str]]:
        """Assemble the `applies_to` dict from the two select fields."""
        if not self.is_valid():
            msg = "applies_to() requires a valid form."
            raise ValueError(msg)
        applies_to: dict[str, list[str]] = {}
        regimes = list(self.cleaned_data["applies_regimes"])
        kinds = list(self.cleaned_data["applies_service_kinds"])
        if regimes:
            applies_to["regimes"] = regimes
        if kinds:
            applies_to["service_kinds"] = kinds
        return applies_to


class NewPlaybookForm(PlaybookForm):
    """The create form: PlaybookForm plus the immutable-at-birth key."""

    key = forms.SlugField(
        label=_("Chave"),
        max_length=64,
        help_text=_("Identificador estável, minúsculo, com hífens."),
    )


class StepForm(forms.Form):
    """One typed step row — every field a real input, no JSON editing."""

    key = forms.SlugField(
        label=_("Chave"),
        max_length=64,
        help_text=_("Identificador estável, minúsculo, usado nas dependências."),
    )
    kind = forms.ChoiceField(
        label=_("Tipo"),
        choices=StepKind.choices,
    )
    label_ptbr = forms.CharField(
        label=_("Etapa"),
        max_length=120,
    )
    client_label_ptbr = forms.CharField(
        label=_("Texto para o cliente"),
        max_length=120,
        required=False,
    )
    depends_on = forms.CharField(
        label=_("Depende de"),
        required=False,
        help_text=_("Chaves separadas por vírgula."),
    )
    owner_rule = forms.ChoiceField(
        label=_("Responsável"),
        choices=OwnerRule.choices,
    )
    due_kind = forms.ChoiceField(
        label=_("Prazo"),
        choices=DUE_KIND_CHOICES,
    )
    due_day = forms.IntegerField(
        label=_("Dia / dias"),
        required=False,
        min_value=0,
        max_value=62,
    )
    month_offset = forms.IntegerField(
        label=_("Meses após"),
        required=False,
        min_value=0,
        max_value=3,
        # No initial: a nonzero default makes the formset's blank extra row
        # "changed", so an untouched add-row would fail required validation
        # instead of being skipped.
    )
    obligation = forms.SlugField(
        label=_("Obrigação"),
        required=False,
        help_text=_("Código como DAS ou DASN."),
    )
    evidence_required = forms.BooleanField(
        label=_("Exige evidência"),
        required=False,
    )
    client_visible = forms.BooleanField(
        label=_("Visível no portal"),
        required=False,
    )
    is_enabled = forms.BooleanField(
        label=_("Ativa"),
        required=False,
    )

    def _due_rule(self) -> dict[str, object]:
        """Assemble the typed inputs into the stored `due_rule` shape."""
        cleaned = self.cleaned_data
        kind = str(cleaned["due_kind"])
        if kind == DueRuleKind.PERIOD_END_OFFSET:
            return {"kind": kind, "days": cleaned.get("due_day") or 0}
        if kind == DueRuleKind.FIXED_DAY:
            return {
                "kind": kind,
                "day": cleaned.get("due_day") or 1,
                "month_offset": cleaned.get("month_offset") or 0,
            }
        return {"kind": kind, "obligation": str(cleaned.get("obligation") or "")}

    def clean(self) -> dict[str, Any]:
        """Validate the assembled rule with the same parser the services run."""
        cleaned = super().clean() or {}
        if self.errors or "due_kind" not in cleaned:
            return cleaned
        try:
            rule = self._due_rule()
            parse_due_rule(rule, step_key=str(cleaned.get("key") or "?"))
        except PlaybookValidationError as exc:
            raise forms.ValidationError(exc.errors) from exc
        cleaned["due_rule"] = rule
        return cleaned

    def to_spec(self) -> StepSpec:
        """Turn the cleaned row into the spec the services validate and write."""
        depends = tuple(
            part.strip()
            for part in str(self.cleaned_data.get("depends_on") or "").split(",")
            if part.strip()
        )
        return StepSpec(
            key=str(self.cleaned_data["key"]),
            kind=str(self.cleaned_data["kind"]),
            label_ptbr=str(self.cleaned_data["label_ptbr"]),
            client_label_ptbr=str(self.cleaned_data.get("client_label_ptbr") or ""),
            depends_on=depends,
            owner_rule=str(self.cleaned_data["owner_rule"]),
            due_rule=dict(self.cleaned_data["due_rule"]),
            evidence_required=bool(self.cleaned_data.get("evidence_required")),
            client_visible=bool(self.cleaned_data.get("client_visible")),
            is_enabled=bool(self.cleaned_data.get("is_enabled")),
        )

    @classmethod
    def initial_of(cls, step: PlaybookStep) -> dict[str, object]:
        """Build form initial data from a stored PlaybookStep."""
        rule = step.due_rule_json or {}
        depends = step.depends_on or []
        initial: dict[str, object] = {
            "key": step.key,
            "kind": step.kind,
            "label_ptbr": step.label_ptbr,
            "client_label_ptbr": step.client_label_ptbr,
            "depends_on": ", ".join(str(key) for key in depends),
            "owner_rule": step.owner_rule,
            "evidence_required": step.evidence_required,
            "client_visible": step.client_visible,
            "is_enabled": step.is_enabled,
            "due_kind": rule.get("kind", DueRuleKind.PERIOD_END_OFFSET.value),
            "obligation": rule.get("obligation", ""),
            "month_offset": rule.get("month_offset", 1),
        }
        if rule.get("kind") == DueRuleKind.FIXED_DAY.value:
            initial["due_day"] = rule.get("day", 1)
        else:
            initial["due_day"] = rule.get("days", 0)
        return initial


StepFormSet = formset_factory(StepForm, extra=1, can_delete=True)
