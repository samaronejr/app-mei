"""Server-validated bulk configuration and exact confirmation form values."""

from django import forms
from django.http import QueryDict

from apps.cycles.bulk import BulkKind
from apps.cycles.bulk_contracts import MAX_ITEMS, PreviewExpectation
from apps.cycles.models import PlaybookVersion, VersionState


class _PlaybookVersionChoiceField(forms.ModelChoiceField[PlaybookVersion]):
    def label_from_instance(self, obj: PlaybookVersion) -> str:
        """Identify the published choice using the playbook editor's wording."""
        return f"{obj.playbook.name} — v{obj.number}"


class BulkConfigForm(forms.Form):
    """Configuration is separate from the explicit subject checkboxes."""

    kind = forms.ChoiceField(
        label="Operação",
        choices=BulkKind.choices,
        widget=forms.Select(attrs={"class": "ui-field-input"}),
    )
    periodo = forms.DateField(
        label="Competência",
        required=False,
        input_formats=["%Y-%m"],
        widget=forms.DateInput(
            format="%Y-%m", attrs={"type": "month", "class": "ui-field-input"}
        ),
    )
    playbook_version_id = _PlaybookVersionChoiceField(
        label="Versão publicada",
        queryset=PlaybookVersion.objects.none(),
        required=False,
        empty_label="Selecione para abrir ou migrar ciclos",
        widget=forms.Select(attrs={"class": "ui-field-input"}),
    )

    def __init__(self, data: QueryDict | None = None) -> None:
        """Populate choices only inside the caller's tenant context."""
        super().__init__(data=data)
        field = self.fields["playbook_version_id"]
        if isinstance(field, forms.ModelChoiceField):
            field.queryset = (
                PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
                .select_related("playbook")
                .order_by("playbook__name", "-number")
            )


class BulkCommandForm(forms.Form):
    """No numeric normalization: 03, +3, spaces and 3.0 are not the typed count 3."""

    action = forms.ChoiceField(
        choices=[
            ("confirm", "Confirmar"),
            ("cancel", "Cancelar"),
            ("resume", "Retomar"),
        ]
    )
    expected_version = forms.IntegerField(min_value=1)
    snapshot_digest = forms.CharField(
        required=False, min_length=64, max_length=64, strip=False
    )
    confirm_count = forms.RegexField(
        regex=r"\A(?:0|[1-9][0-9]*)\Z",
        max_length=len(str(MAX_ITEMS)),
        required=False,
        strip=False,
    )

    def expectation(self) -> PreviewExpectation:
        """Return the two checked identity fields as one typed concurrency token."""
        if (
            not self.cleaned_data["confirm_count"]
            or not self.cleaned_data["snapshot_digest"]
        ):
            msg = "Digite a quantidade exata de itens para confirmar."
            raise forms.ValidationError(msg)
        return PreviewExpectation(
            self.cleaned_data["expected_version"], self.cleaned_data["snapshot_digest"]
        )
