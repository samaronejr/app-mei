"""AXRM-018: each federal health code shows its own message on the automations page.

The page's message chain has one branch per code and no fallback branch; every row then
prints its code. A federal row whose code is `pausado`, `ambiente_nao_aprovado`,
`federal_sweep_failed` or `federal_sweep_running` must render its own branch: with the
code masked, its message is branch text followed by exactly what an unregistered code
renders (the code line alone), and no two codes share a branch. Wording is not pinned.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from django.utils import timezone

from apps.obligations.models import SchedulerHeartbeat
from tests.delivery.test_boundaries import _web

if TYPE_CHECKING:
    from django.test import Client
    from pytest_django.fixtures import SettingsWrapper

    from tests.delivery.conftest import DeliverySetup

pytestmark = pytest.mark.django_db(transaction=True)

CODES = (
    "pausado",
    "ambiente_nao_aprovado",
    "federal_sweep_failed",
    "federal_sweep_running",
)
NAMES = ("schedule_payment_consultations", "schedule_situation_reports")
UNREGISTERED = "axrm018_unregistered_code"


def _message(web: Client, code: str) -> str:
    """The rendered message of both federal rows when their heartbeat carries `code`."""
    for name in NAMES:
        SchedulerHeartbeat.objects.update_or_create(
            name=name,
            defaults={"last_attempt_at": timezone.now(), "last_error_code": code},
        )
    response = web.get("/gestao/automacoes/")
    assert response.status_code == 200
    pattern = rf'data-job-error="{re.escape(code)}">(.*?)</p>'
    found = re.findall(pattern, response.content.decode(), re.DOTALL)
    assert len(found) == len(NAMES), found
    texts = {" ".join(re.sub(r"<[^>]+>", " ", text).split()) for text in found}
    assert len(texts) == 1, texts
    # Mask the row's own code so only the branch text can tell codes apart.
    return texts.pop().replace(code, "<code>")


def test_each_federal_health_code_renders_its_own_message(
    delivery: DeliverySetup, settings: SettingsWrapper
) -> None:
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    # One signed-in client for every render: a second enrolment would duplicate it.
    web = _web(delivery, delivery.graph.actor)
    messages = {code: _message(web, code) for code in CODES}
    no_branch = _message(web, UNREGISTERED)
    assert all(
        m.endswith(no_branch) and len(m) > len(no_branch) for m in messages.values()
    ), (
        no_branch,
        messages,
    )
    assert len(set(messages.values())) == len(CODES), messages
