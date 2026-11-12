"""Shared acceptance boundaries; missing implementation is an assertion, not a skip."""

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
CONTRATANTE = "12345678000195"
FIRM = "11222333000181"
CLIENT = "12ABC34501DE35"


def serpro_module(name: str) -> ModuleType:
    assert importlib.util.find_spec("apps.integrations.serpro") is not None, (
        "AXRM-013 must supply the SERPRO package"
    )
    qualified = f"apps.integrations.serpro.{name}"
    assert importlib.util.find_spec(qualified) is not None, (
        f"AXRM-013 must supply {qualified}"
    )
    return importlib.import_module(qualified)


def broker_module() -> ModuleType:
    path = ROOT / "ops" / "serpro-broker" / "broker.py"
    assert path.is_file(), "AXRM-013 must supply the isolated broker"
    name = "axrm013_acceptance_broker"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        sys.path.insert(0, str(path.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(path.parent))
    return sys.modules[name]
