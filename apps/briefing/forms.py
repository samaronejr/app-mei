"""Parse the same bounded filters for URLs and saved preferences."""

from collections.abc import Mapping
from typing import Final

from django import forms
from django.core.exceptions import ValidationError

from apps.briefing.rules import RULE_LABELS
from apps.briefing.types import BriefingFilters, BriefingScope
from apps.core.templatetags.ptbr import OBLIGATION_STATUS_LABELS
from apps.cycles.forms import REGIME_CHOICES, SERVICE_KIND_CHOICES
from apps.cycles.models import ExceptionState, IntakeState, ProposalState, RequestState
from apps.delivery.models import MessageState
from apps.obligations.coverage import CoverageState

WORK_STATES: Final = dict(
    [
        *((code, str(label)) for code, label in OBLIGATION_STATUS_LABELS.items()),
        *RequestState.choices,
        *ExceptionState.choices,
        *ProposalState.choices,
        *MessageState.choices,
        *IntakeState.choices,
        (CoverageState.STALE, "Desatualizada"),
        (CoverageState.REFRESH_FAILED, "Falha na verificação"),
    ]
)


class BriefingFilterForm(forms.Form):
    """Filters only narrow the freshly authorized portfolio."""

    escopo = forms.ChoiceField(
        label="Fila",
        choices=[("team", "Equipe"), ("me", "Minhas")],
        required=False,
    )
    responsavel = forms.UUIDField(label="Responsável", required=False)
    cliente = forms.UUIDField(required=False, widget=forms.HiddenInput)
    servico = forms.ChoiceField(
        label="Serviço",
        choices=[("", "Todos os serviços"), *SERVICE_KIND_CHOICES],
        required=False,
    )
    regime = forms.ChoiceField(
        label="Regime",
        choices=[("", "Todos os regimes"), *REGIME_CHOICES],
        required=False,
    )
    uf = forms.RegexField(label="UF", regex=r"^[A-Z]{2}$", max_length=2, required=False)
    municipio = forms.RegexField(
        label="Código IBGE do município",
        regex=r"^[0-9]{7}$",
        max_length=7,
        required=False,
    )
    periodo = forms.DateField(
        label="Competência",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(attrs={"type": "month"}, format="%Y-%m"),
        required=False,
    )
    estado = forms.ChoiceField(
        label="Estado do trabalho",
        choices=[("", "Todos os estados"), *WORK_STATES.items()],
        required=False,
    )
    motivo = forms.ChoiceField(
        label="Motivo da prioridade",
        choices=[("", "Todos os motivos"), *RULE_LABELS.items()],
        required=False,
    )
    fonte = forms.ChoiceField(
        label="Atualidade da fonte",
        choices=[
            ("", "Todas as fontes"),
            (CoverageState.UNKNOWN, "Sem verificação externa"),
            (CoverageState.STALE, "Desatualizada"),
            (CoverageState.REFRESH_FAILED, "Falha na verificação"),
        ],
        required=False,
    )

    def selection(self) -> tuple[BriefingScope, BriefingFilters]:
        """Return typed narrowing values, or a field-specific validation error."""
        if not self.is_valid():
            raise ValidationError(self.errors.as_text())
        data = self.cleaned_data
        scope: BriefingScope = data["escopo"] or "team"
        return scope, BriefingFilters(
            responsible=data["responsavel"],
            client_id=data["cliente"],
            service=data["servico"],
            regime=data["regime"],
            uf=data["uf"],
            municipality=data["municipio"],
            period=data["periodo"],
            work_state=data["estado"],
            risk_reason=data["motivo"],
            freshness=data["fonte"],
        )


def parse_selection(data: Mapping[str, str]) -> tuple[BriefingScope, BriefingFilters]:
    """Refuse extra JSON keys rather than persisting an unrecognized authority hint."""
    if set(data) - set(BriefingFilterForm.base_fields):
        message = "A visualização contém filtros desconhecidos."
        raise ValidationError(message)
    return BriefingFilterForm(data).selection()


def selection_values(scope: BriefingScope, filters: BriefingFilters) -> dict[str, str]:
    """Serialize only the validated filter contract, never a cursor or permission."""
    return {
        "escopo": scope,
        "responsavel": str(filters.responsible) if filters.responsible else "",
        "cliente": str(filters.client_id) if filters.client_id else "",
        "servico": filters.service,
        "regime": filters.regime,
        "uf": filters.uf,
        "municipio": filters.municipality,
        "periodo": filters.period.strftime("%Y-%m") if filters.period else "",
        "estado": filters.work_state,
        "motivo": filters.risk_reason,
        "fonte": filters.freshness,
    }
