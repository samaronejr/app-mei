"""The data-subject-rights intake form."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.audit.models import (
    DataSubjectRelationship,
    DataSubjectRequest,
    DataSubjectRequestType,
)


class DataSubjectRequestForm(forms.ModelForm[DataSubjectRequest]):
    """Collect an LGPD art. 18 rights request from someone who may have no account."""

    relationship = forms.ChoiceField(
        label=_("Relacionamento"),
        choices=DataSubjectRelationship.choices,
    )
    request_type = forms.ChoiceField(
        label=_("Tipo de solicitação"),
        choices=DataSubjectRequestType.choices,
    )

    class Meta:
        """Form metadata."""

        model = DataSubjectRequest
        fields = (
            "requester_name",
            "cpf",
            "email",
            "relationship",
            "request_type",
            "detail",
        )
