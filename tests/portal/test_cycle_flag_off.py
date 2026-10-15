"""Flag-off portal output must match the independently captured final-T8 templates.

Fixtures were captured verbatim from 62771a9 with git show, not derived from the
implementation under test. Both the parent and child are frozen so an inherited
layout change cannot evade the comparison by rendering through today's parent.
"""

from pathlib import Path

import pytest
from django.db import connection
from django.template import Context, Engine
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from tests.obligations.test_payment_observation_labels import _firm

pytestmark = pytest.mark.django_db(transaction=True)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("kill_switch", [False, True])
@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
def test_unflagged_portal_home_matches_pre_cycle_parent_and_child_bytes(
    settings: SettingsWrapper, kill_switch: bool
) -> None:
    tenant, _, owner = _firm(
        "cycle-compat", features=["cycles_r1"] if kill_switch else []
    )
    settings.AXORIM_DISABLED_FEATURES = ["cycles_r1"] if kill_switch else []
    http = Client()
    http.force_login(owner)
    with CaptureQueriesContext(connection) as queries:
        response = http.get("/", headers={"host": f"{tenant.slug}-portal.localhost"})
    assert response.status_code == 200
    assert not any("cycles_servicecycle" in query["sql"] for query in queries)
    engine = Engine.get_default()
    legacy = Engine(
        dirs=engine.dirs,
        libraries=engine.libraries,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "portal/base.html": (FIXTURES / "base_pre_cycles.html").read_text(),
                    "portal/home.html": (FIXTURES / "home_pre_cycles.html").read_text(),
                },
            ),
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    )
    context = {
        name: response.context[name]
        for name in (
            "client",
            "obligation_count",
            "overdue_count",
            "next_action",
            "messages",
        )
    }
    context.update(
        request=response.wsgi_request, csrf_token=str(response.context["csrf_token"])
    )
    with override_settings(ROOT_URLCONF="apps.portal.urls"):
        before = legacy.get_template("portal/home.html").render(Context(context))
    assert response.content == before.encode()
