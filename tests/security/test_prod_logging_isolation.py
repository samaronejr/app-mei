"""Loading the production settings must not reach into the shared base config."""

import importlib

from config.settings import base, prod


def test_loading_prod_settings_leaves_the_shared_logging_config_alone() -> None:
    # Given the production settings module loaded again, as an import would.
    importlib.reload(prod)
    # Then the base config every other settings module shares has no console filter.
    assert "credential_urls" not in base.LOGGING.get("filters", {})
    assert "filters" not in base.LOGGING["handlers"]["console"]
    # And production carries its own copy with the filter installed.
    assert prod.LOGGING is not base.LOGGING
    assert prod.LOGGING["handlers"]["console"]["filters"] == ["credential_urls"]
