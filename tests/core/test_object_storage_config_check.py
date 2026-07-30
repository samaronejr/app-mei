"""`core.E011` refuses a deployment that selects object storage without configuring it.

The requirement is enforced at boot rather than at import. An import-time raise names
only the first missing variable, makes the settings module unimportable for inspection —
which broke a cookie-policy test that reads it — and cannot itself be tested.

This check is inert under the filesystem storage development and tests run on, which is
why the pair below matters: without a case that makes it fire, it would pass everywhere
today and keep passing if it stopped working.
"""

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.checks import _object_storage_config_errors

S3 = "storages.backends.s3.S3Storage"
FILESYSTEM = "django.core.files.storage.FileSystemStorage"

COMPLETE = {
    "bucket_name": "meiapp-private-files-prod",
    "endpoint_url": "https://example.invalid",
    "region_name": "sa-vinhedo-1",
    "access_key": "key",
    "secret_key": "secret",
}


def _storages(backend: str, options: dict[str, str] | None = None) -> dict[str, object]:
    default: dict[str, object] = {"BACKEND": backend}
    if options is not None:
        default["OPTIONS"] = options
    return {"default": default, "staticfiles": {"BACKEND": FILESYSTEM}}


def test_the_check_is_silent_on_filesystem_storage(settings: SettingsWrapper) -> None:
    # Given the filesystem backend development and tests run on
    settings.STORAGES = _storages(FILESYSTEM)

    # Then the check does not fire — it governs object storage only
    assert _object_storage_config_errors() == []


def test_the_check_is_silent_when_object_storage_is_fully_configured(
    settings: SettingsWrapper,
) -> None:
    # Given S3 selected with every option populated
    settings.STORAGES = _storages(S3, COMPLETE)

    # Then it passes, so the report below is not simply "any S3 config is an error"
    assert _object_storage_config_errors() == []


@pytest.mark.parametrize("missing", sorted(COMPLETE))
def test_the_check_names_each_option_left_empty(
    settings: SettingsWrapper,
    missing: str,
) -> None:
    # Given S3 selected with exactly one option blank
    options = dict(COMPLETE)
    options[missing] = ""
    settings.STORAGES = _storages(S3, options)

    # When the check runs
    errors = _object_storage_config_errors()

    # Then it refuses to boot and names the one that is missing
    assert len(errors) == 1
    assert errors[0].id == "core.E011"
    assert missing in str(errors[0].msg)


def test_the_check_names_every_missing_option_at_once(
    settings: SettingsWrapper,
) -> None:
    # Given S3 selected with nothing configured, the shape a first deploy produces
    settings.STORAGES = _storages(S3, dict.fromkeys(COMPLETE, ""))

    # When the check runs
    errors = _object_storage_config_errors()

    # Then one message lists all five, rather than an import-time raise that would
    # surface them one redeploy at a time
    assert len(errors) == 1
    for option in COMPLETE:
        assert option in str(errors[0].msg)


def test_the_check_fires_when_options_are_absent_entirely(
    settings: SettingsWrapper,
) -> None:
    # Given S3 selected with no OPTIONS mapping at all
    settings.STORAGES = _storages(S3)

    # Then it refuses rather than reading absence as satisfaction
    assert len(_object_storage_config_errors()) == 1
