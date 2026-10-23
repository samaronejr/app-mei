"""Guard the R1 certificate metadata-only boundary across every model."""

import re

from django.apps import apps
from django.conf import settings
from django.db import models

from apps.integrations.models import ProviderConnection
from apps.integrations.validators import validate_secret_ref

CERTIFICATE_NAME = re.compile(r"(?:cert|certificado|certificate)", re.IGNORECASE)
MATERIAL_NAME = re.compile(
    r"(?:password|senha|pem|key|chave|pfx|p12|blob|file|arquivo|der|private)",
    re.IGNORECASE,
)
STANDALONE_SECRET_NAME = re.compile(
    r"(?:private_key|chave_privada|pfx|p12|cert_password|senha_certificado)",
    re.IGNORECASE,
)
BINARY_OR_FILE_FIELDS = (models.BinaryField, models.FileField)
# AXRM-007 (ADR-0008, INV-R7): provider records hold status and secret NAMES only.
PROVIDER_VALUE_NAME = re.compile(
    r"(?:pfx|p12|private|senha|password|token)", re.IGNORECASE
)
PROVIDER_SETTING_PREFIXES = ("PROVIDER", "SERPRO", "FOCUS")


def _forbidden_fields() -> list[str]:
    offenders: list[str] = []
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if not hasattr(field, "name"):
                continue
            name = field.name
            if (
                STANDALONE_SECRET_NAME.search(name)
                or (CERTIFICATE_NAME.search(name) and MATERIAL_NAME.search(name))
                or (
                    CERTIFICATE_NAME.search(name)
                    and isinstance(field, BINARY_OR_FILE_FIELDS)
                )
            ):
                offenders.append(f"{model._meta.label}.{name}")
    return offenders


def test_certificate_material_is_metadata_only() -> None:
    """No model may expose a field suitable for certificate material or secrets."""
    assert not _forbidden_fields()


def test_provider_records_hold_no_secret_value_field() -> None:
    """AXRM-007: no integrations field may be shaped to carry key material."""
    integration_models = list(apps.get_app_config("integrations").get_models())
    assert integration_models, "the scan found no integrations model"
    offenders = [
        f"{model._meta.label}.{field.name}"
        for model in integration_models
        for field in model._meta.get_fields()
        if PROVIDER_VALUE_NAME.search(field.name)
        or isinstance(field, BINARY_OR_FILE_FIELDS)
    ]
    assert not offenders


def test_provider_settings_name_no_secret_value() -> None:
    """AXRM-007: provider settings are switches, environments and caps."""
    names = [
        name for name in dir(settings) if name.startswith(PROVIDER_SETTING_PREFIXES)
    ]
    assert "PROVIDER_REAL_DATA_APPROVED" in names
    assert not [name for name in names if PROVIDER_VALUE_NAME.search(name)]


def test_the_secret_reference_is_validated_as_a_name() -> None:
    """AXRM-007: `secret_ref` is a deployment secret's name, never its value."""
    field = ProviderConnection._meta.get_field("secret_ref")
    assert validate_secret_ref in field.validators
