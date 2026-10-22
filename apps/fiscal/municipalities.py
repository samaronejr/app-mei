"""Registry validation at the client form and import boundaries."""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.fiscal.models import Municipality


def validate_municipality_code(value: str) -> None:
    """Refuse a non-empty client municipality absent from the shared registry."""
    if value and not Municipality.objects.filter(ibge_code=value).exists():
        raise ValidationError(
            _("Município não encontrado no cadastro (código IBGE)"),
            code="municipality_not_found",
        )
