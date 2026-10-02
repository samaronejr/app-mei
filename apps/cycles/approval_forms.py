"""Forms carry exact selections and optimistic revisions, never arbitrary JSON."""

from typing import cast
from uuid import UUID

from django import forms
from django.utils import timezone

from apps.clients.models import ClientContact
from apps.cycles.effects.inputs import MoneyBinding, ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    EvidenceLink,
    EvidenceRequest,
    ExtractedField,
    ServiceCycle,
)
from apps.obligations.models import (
    ObservationKind,
    ObservationSourceKind,
    ObservationVerificationStatus,
    SourceObservation,
)

ACTION_LABELS = {
    ActionKind.CLIENT_PUBLICATION: "Publicar documentos para o cliente",
    ActionKind.EMAIL_NOTICE: "Enviar e-mail",
    ActionKind.CYCLE_COMPLETION: "Concluir ciclo",
    ActionKind.REQUEST_WAIVER: "Dispensar solicitação",
}
OBSERVATION_KIND_LABELS: dict[str, str] = {
    ObservationKind.PAYMENT: "Pagamento",
    ObservationKind.FILING: "Entrega",
    ObservationKind.GUIDE_ISSUED: "Emissão de guia",
}
OBSERVATION_SOURCE_LABELS: dict[str, str] = {
    ObservationSourceKind.OFFICIAL_API: "Consulta oficial",
    ObservationSourceKind.OFFICIAL_FILE_IMPORT: "Arquivo de fonte oficial",
    ObservationSourceKind.CLIENT_SUBMITTED: "Enviado pelo cliente",
    ObservationSourceKind.STAFF_RECORDED: "Registrado pela equipe",
    ObservationSourceKind.DETERMINISTIC_RULE: "Regra determinística",
    ObservationSourceKind.AI_INFERENCE: "Sugestão de IA",
    ObservationSourceKind.LEGACY_STATUS: "Registro anterior",
}
OBSERVATION_STATUS_LABELS: dict[str, str] = {
    ObservationVerificationStatus.VERIFIED: "Verificado pela fonte",
    ObservationVerificationStatus.PENDING_REVIEW: "Aguardando revisão",
    ObservationVerificationStatus.UNKNOWN: "Sem verificação",
    ObservationVerificationStatus.UNSUPPORTED: "Sem suporte da fonte",
    ObservationVerificationStatus.STALE: "Desatualizado",
    ObservationVerificationStatus.REFRESH_FAILED: "Atualização não concluída",
    ObservationVerificationStatus.UNAUTHORIZED: "Sem autorização",
    ObservationVerificationStatus.NOT_APPLICABLE: "Não aplicável",
}


def observation_status_label(source: str, status: str) -> str:
    """Never present a manual assertion as an official verification."""
    if status == ObservationVerificationStatus.VERIFIED and source not in {
        ObservationSourceKind.OFFICIAL_API,
        ObservationSourceKind.OFFICIAL_FILE_IMPORT,
    }:
        return "Registro não oficial"
    return OBSERVATION_STATUS_LABELS.get(status, status)


class ProposalForm(forms.Form):
    """Select real client-scoped records before showing their frozen effect."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    previous_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    previous_version = forms.IntegerField(required=False, widget=forms.HiddenInput)
    action_kind = forms.ChoiceField(label="Ação", choices=tuple(ACTION_LABELS.items()))
    step_key = forms.SlugField(label="Etapa", max_length=64, initial="manual")
    occurrence = forms.IntegerField(label="Ocorrência", min_value=1, initial=1)
    recipients = forms.MultipleChoiceField(
        label="Destinatários", required=False, widget=forms.CheckboxSelectMultiple
    )
    document_links = forms.MultipleChoiceField(
        label="Documentos", required=False, widget=forms.CheckboxSelectMultiple
    )
    evidence_requests = forms.MultipleChoiceField(
        label="Solicitações de evidência",
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    observations = forms.MultipleChoiceField(
        label="Observações de fonte",
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    money = forms.MultipleChoiceField(
        label="Valores extraídos",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Valores em BRL, escala 2, arredondamento ROUND_HALF_UP.",
    )
    subject = forms.CharField(label="Assunto", max_length=255, required=False)
    body = forms.CharField(
        label="Mensagem completa",
        widget=forms.Textarea,
        max_length=10000,
        required=False,
        help_text="Este texto será congelado, sem regeneração na aprovação.",
    )
    reason = forms.CharField(
        label="Motivo ou resultado pretendido",
        widget=forms.Textarea,
        max_length=2000,
        required=False,
    )
    not_before = forms.DateTimeField(
        label="Executar somente a partir de",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    expires_at = forms.DateTimeField(
        label="Validade da aprovação",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
        help_text=(
            "Horário de São Paulo. A aprovação expira mesmo sem mudança de dados."
        ),
    )

    def set_choices(self, cycle: ServiceCycle) -> None:
        """Constrain selections to this already-authorized cycle and client."""
        cast("forms.MultipleChoiceField", self.fields["recipients"]).choices = [
            (str(row.pk), f"{row.name} · {row.email}")
            for row in ClientContact.objects.filter(client_id=cycle.client_id).order_by(
                "name", "pk"
            )
        ]
        links = list(
            EvidenceLink.objects.filter(request__cycle=cycle)
            .select_related("document")
            .order_by("pk")
        )
        cast("forms.MultipleChoiceField", self.fields["document_links"]).choices = [
            (
                str(row.pk),
                (
                    f"{row.document.original_filename} · "
                    f"{row.get_review_decision_display()}"
                ),
            )
            for row in links
        ]
        cast("forms.MultipleChoiceField", self.fields["evidence_requests"]).choices = [
            (str(row.pk), f"{row.title} · {row.get_state_display()}")
            for row in EvidenceRequest.objects.filter(cycle=cycle).order_by("pk")
        ]
        cast("forms.MultipleChoiceField", self.fields["observations"]).choices = [
            (
                str(row.pk),
                (
                    f"{OBSERVATION_KIND_LABELS[row.kind]} · "
                    f"{OBSERVATION_SOURCE_LABELS[row.source_kind]} · "
                    + observation_status_label(row.source_kind, row.verification_status)
                    + f" · {timezone.localtime(row.observed_at):%d/%m/%Y %H:%M}"
                ),
            )
            for row in SourceObservation.objects.filter(
                client_id=cycle.client_id, period=cycle.period_start
            ).order_by("-observed_at", "pk")
        ]
        amounts = set(
            ExtractedField.objects.filter(
                document_id__in={row.document_id for row in links},
                name__in=["valor_total", "valor_servico"],
            ).values_list("document_id", "name")
        )
        cast("forms.MultipleChoiceField", self.fields["money"]).choices = [
            (f"{link.pk}:{name}", f"{link.document.original_filename} · {name}")
            for link in links
            for name in ("valor_total", "valor_servico")
            if (link.document_id, name) in amounts
        ]
        for field in self.fields.values():
            if not isinstance(
                field.widget, (forms.CheckboxSelectMultiple, forms.HiddenInput)
            ):
                field.widget.attrs["class"] = "ui-field-input"

    def draft(self) -> ProposalDraft:
        """Return typed ids and rendered text after Django has validated the form."""
        data = self.cleaned_data
        money = []
        for value in data["money"]:
            link, name = value.split(":", 1)
            money.append(MoneyBinding(UUID(link), name))
        return ProposalDraft(
            ProposalInput(
                action_kind=ActionKind(data["action_kind"]),
                step_key=data["step_key"],
                occurrence=data["occurrence"],
                recipient_ids=tuple(UUID(value) for value in data["recipients"]),
                document_link_ids=tuple(
                    UUID(value) for value in data["document_links"]
                ),
                evidence_request_ids=tuple(
                    UUID(value) for value in data["evidence_requests"]
                ),
                observation_ids=tuple(UUID(value) for value in data["observations"]),
                money=tuple(money),
                subject=data["subject"],
                body=data["body"],
                reason=data["reason"],
            ),
            data["expires_at"],
            not_before=data["not_before"],
            previous_id=data["previous_id"],
            previous_version=data["previous_version"],
        )


class DecisionForm(forms.Form):
    """Carry the version the approver actually saw."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        choices=[
            ("approve", "approve"),
            ("reject", "reject"),
            ("cancel", "cancel"),
            ("revalidate", "revalidate"),
        ]
    )
    reason = forms.CharField(max_length=2000, required=False)
    self_approval_acknowledged = forms.BooleanField(required=False)


class ApprovalPolicyForm(forms.Form):
    """An explicit acknowledgment, never an implicit default for a small firm."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    allow_self_approval = forms.BooleanField(
        label="Permitir autoaprovação quando houver uma única pessoa ativa",
        required=False,
    )
    acknowledged = forms.BooleanField(
        label="Entendo que a mesma pessoa poderá propor e aprovar os efeitos",
        required=False,
    )
    quorum = forms.IntegerField(
        label="Aprovações necessárias",
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={"class": "ui-field-input"}),
    )
