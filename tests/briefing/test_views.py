"""Drive the real Hoje route and its version-carrying preference forms."""

from http import HTTPStatus
from uuid import uuid4

import pytest
from django.urls import reverse

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.models import BriefingFeedback, SavedView
from tests.briefing.fixtures import NOW
from tests.briefing.http_fixtures import http_firm
from tests.ui.refusal_contract import assert_readable_page

pytestmark = pytest.mark.django_db(transaction=True)


def test_flagged_dashboard_renders_the_scoped_queue_and_injected_time() -> None:
    firm = http_firm()

    response = firm.as_owner().get(reverse("dashboard"))

    assert response.status_code == 200
    assert b'id="briefing-content"' in response.content
    assert b'data-briefing-kind="obligation"' in response.content
    assert response.context["page"].total == 1
    assert response.context["page"].computed_at == NOW


def test_saved_view_reauthorizes_on_the_next_http_request() -> None:
    firm = http_firm()
    session = firm.as_accountant()
    saved = session.post(
        reverse("briefing-save-view"),
        {
            "name": "Minha seleção",
            "expected_version": "0",
            "cliente": str(firm.clients[0].pk),
            "escopo": "team",
        },
    )
    assert saved.status_code == 302
    with tenant_context(firm.tenant.pk):
        preference = SavedView.objects.get(user=firm.accountant)
    control = session.get(reverse("dashboard"), {"vista": str(preference.pk)})
    assert control.context["page"].total == 1
    with tenant_context(firm.tenant.pk):
        ClientAssignment.objects.filter(user=firm.accountant).delete()

    response = session.get(reverse("dashboard"), {"vista": str(preference.pk)})

    assert response.status_code == 200
    assert response.context["page"].total == 0
    assert firm.clients[0].legal_name.encode() not in response.content


def test_dismissal_post_records_a_reason_and_rejects_replayed_form() -> None:
    firm = http_firm()
    session = firm.as_owner()
    page = session.get(reverse("dashboard")).context["page"]
    item = page.items[0]
    payload = {
        "kind": item.kind,
        "subject_ref": str(item.subject_ref),
        "fact_version": item.fact_version,
        "reason": "Já conferido nesta versão",
    }
    response = session.post(reverse("briefing-dismiss"), payload)
    assert response.status_code == 302
    assert session.get(reverse("dashboard")).context["page"].total == 0

    stale = session.post(reverse("briefing-dismiss"), payload)

    assert stale.status_code == 409
    with tenant_context(firm.tenant.pk):
        assert BriefingFeedback.objects.count() == 1


def test_a_refused_dismissal_reads_in_plain_sentences() -> None:
    """AXR1-054: the dismissal service's ValidationError reads as its sentence."""
    firm = http_firm()
    response = firm.as_owner().post(
        reverse("briefing-dismiss"),
        {
            "kind": "coverage",
            "subject_ref": str(uuid4()),
            "fact_version": "1",
            "reason": "Conferido nesta versão",
        },
    )
    assert_readable_page(
        response,
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        reason="Informe a competência da cobertura.",
    )


@pytest.mark.parametrize(
    "query",
    [
        {"responsavel": "not-a-uuid"},
        {"periodo": "2026-13"},
        {"cursor": "not-a-signed-cursor"},
        {"escopo": "all"},
    ],
)
def test_malformed_filter_or_cursor_has_a_recovery_response(
    query: dict[str, str],
) -> None:
    firm = http_firm()

    response = firm.as_owner().get(reverse("dashboard"), query)

    assert response.status_code == 422
    assert b'id="briefing-error"' in response.content


def test_invalid_return_selection_does_not_commit_a_dismissal() -> None:
    firm = http_firm()
    session = firm.as_owner()
    item = session.get(reverse("dashboard")).context["page"].items[0]

    response = session.post(
        reverse("briefing-dismiss"),
        {
            "kind": item.kind,
            "subject_ref": str(item.subject_ref),
            "fact_version": item.fact_version,
            "reason": "Conferido",
            "escopo": "all",
        },
    )

    assert response.status_code == 422
    with tenant_context(firm.tenant.pk):
        assert not BriefingFeedback.objects.exists()


def test_invalid_return_selection_does_not_delete_a_saved_view() -> None:
    firm = http_firm()
    session = firm.as_owner()
    session.post(
        reverse("briefing-save-view"),
        {"name": "Revisão", "expected_version": "0"},
    )
    with tenant_context(firm.tenant.pk):
        saved = SavedView.objects.get(user=firm.owner)

    response = session.post(
        reverse("briefing-delete-view", args=[saved.pk]),
        {"expected_version": str(saved.version), "periodo": "invalid"},
    )

    assert response.status_code == 422
    with tenant_context(firm.tenant.pk):
        assert SavedView.objects.filter(pk=saved.pk).exists()


def test_invalid_stored_selection_has_a_recovery_response() -> None:
    firm = http_firm()
    with tenant_context(firm.tenant.pk):
        saved = SavedView.objects.create(
            tenant=firm.tenant,
            user=firm.owner,
            name="Seleção antiga",
            filters_json={"removed_filter": "value"},
        )

    response = firm.as_owner().get(reverse("dashboard"), {"vista": str(saved.pk)})

    assert response.status_code == 422
    assert b'id="briefing-error"' in response.content
