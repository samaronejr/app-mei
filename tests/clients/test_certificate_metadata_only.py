"""Guard the R1 certificate metadata-only boundary across every model."""

import re

from django.apps import apps
from django.db import models

CERTIFICATE_NAME = re.compile(
    r"(?:cert|certificado|pfx|p12|private_key|chave_privada)",
    re.IGNORECASE,
)
SECRET_NAME = re.compile(
    r"(?:private_key|chave_privada|pfx|p12|cert_password|senha_certificado)",
    re.IGNORECASE,
)
BINARY_OR_FILE_FIELDS = (models.BinaryField, models.FileField)


def _forbidden_fields() -> list[str]:
    offenders: list[str] = []
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if not hasattr(field, "name"):
                continue
            name = field.name
            if SECRET_NAME.search(name) or (
                CERTIFICATE_NAME.search(name)
                and isinstance(field, BINARY_OR_FILE_FIELDS)
            ):
                offenders.append(f"{model._meta.label}.{name}")
    return offenders


def test_certificate_material_is_metadata_only() -> None:
    """No model may expose a field suitable for certificate material or secrets."""
    assert not _forbidden_fields()
