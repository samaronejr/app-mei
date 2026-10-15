"""Refused creation preserves the form without domain or audit writes."""

from http import HTTPStatus

import pytest
from django.urls import reverse

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles.errors import (
    IllegalTransition,
    ImmutableVersion,
    PlaybookValidationError,
    VersionConflict,
)
from apps.cycles.models import Playbook, PlaybookStep, PlaybookVersion, VersionState
from apps.cycles.services import playbooks
from tests.ui.factories import Firm, make_firm
from tests.ui.test_unsaved_guard_forms import _guarded

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def firm() -> Firm:
    firm = make_firm("bound-create")
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    return firm


def _data(key: str = "retained-key") -> dict[str, str | list[str]]:
    return {
        "action": "create",
        "name": "Retained name",
        "key": key,
        "applies_regimes": ["mei", "simples"],
        "applies_service_kinds": ["mei_mensal", "outro"],
    }


def _snapshot(firm: Firm) -> tuple[object, ...]:
    with tenant_context(firm.tenant.pk):
        return (
            list(Playbook.objects.order_by("pk").values()),
            list(PlaybookVersion.objects.order_by("pk").values()),
            list(PlaybookStep.objects.order_by("pk").values()),
            list(Event.objects.order_by("pk").values()),
        )


@pytest.mark.parametrize("key", ["bad key!", ""])
def test_invalid_field_retains_values_and_inline_errors_without_writes(
    firm: Firm, key: str
) -> None:
    session = firm.as_owner()
    before = _snapshot(firm)
    data = _data(key)

    response = session.post(reverse("playbook-list"), data)

    assert _snapshot(firm) == before
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    form = response.context["create_form"]
    assert form.is_bound
    assert form.errors.keys() == {"key"}
    for field in ("name", "key", "applies_regimes", "applies_service_kinds"):
        assert form[field].value() == data[field]
    html = response.content.decode()
    for value in ("mei", "simples", "mei_mensal", "outro"):
        assert f'<option value="{value}" selected>' in html
    assert 'id="id_key_error"' in html
    assert 'aria-invalid="true"' in html
    assert _guarded(response.content) == {"create": True}


def test_duplicate_key_is_a_bound_conflict_and_corrected_retry_succeeds(
    firm: Firm,
) -> None:
    existing = playbooks.create_playbook(
        actor=firm.owner, tenant=firm.tenant, key="retained-key", name="Original"
    )
    session = firm.as_owner()
    session.raise_request_exception = False
    before = _snapshot(firm)
    url = reverse("playbook-list")

    refused = session.post(url, _data())

    assert _snapshot(firm) == before
    assert refused.status_code == HTTPStatus.CONFLICT
    form = refused.context["create_form"]
    assert form.errors.keys() == {"key"}
    assert form["key"].value() == existing.key
    assert form["name"].value() == "Retained name"
    assert 'id="id_key_error"' in refused.content.decode()
    for value in ("mei", "simples", "mei_mensal", "outro"):
        assert f'<option value="{value}" selected>' in refused.content.decode()
    assert _guarded(refused.content) == {"create": True}

    # Client.post encodes dict values, not QueryDict's multi-value lists.
    retry = dict(form.data.lists())
    retry["key"] = ["corrected-key"]
    accepted = session.post(url, retry)

    assert accepted.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        created = Playbook.objects.get(key="corrected-key")
        assert accepted["Location"] == reverse("playbook-detail", args=[created.pk])
        assert created.name == "Retained name"
        version = created.versions.get()
        assert version.state == VersionState.DRAFT
        assert version.applies_to_json == {
            "regimes": ["mei", "simples"],
            "service_kinds": ["mei_mensal", "outro"],
        }
        existing.refresh_from_db()
        assert existing.name == "Original"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (PlaybookValidationError(["Refused scope"]), HTTPStatus.UNPROCESSABLE_ENTITY),
        (VersionConflict("Concurrent change"), HTTPStatus.CONFLICT),
        (IllegalTransition("Refused transition"), HTTPStatus.CONFLICT),
        (ImmutableVersion("Read only"), HTTPStatus.CONFLICT),
    ],
)
def test_service_refusal_rolls_back_and_retains_non_field_errors(
    firm: Firm,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: HTTPStatus,
) -> None:
    # Refuse after the real service inserts the identity, inside its transaction.
    def refuse_validation(**kwargs: object) -> None:
        raise error

    monkeypatch.setattr(playbooks, "validate_draft", refuse_validation)
    session = firm.as_owner()
    before = _snapshot(firm)
    data = _data()

    response = session.post(reverse("playbook-list"), data)

    assert _snapshot(firm) == before
    assert response.status_code == status
    form = response.context["create_form"]
    assert form.non_field_errors()
    for field in ("name", "key", "applies_regimes", "applies_service_kinds"):
        assert form[field].value() == data[field]
    assert 'role="alert"' in response.content.decode()
    for value in ("mei", "simples", "mei_mensal", "outro"):
        assert f'<option value="{value}" selected>' in response.content.decode()
    assert _guarded(response.content) == {"create": True}


@pytest.mark.parametrize("flagged", [True, False])
def test_creation_keeps_live_flag_and_capability_denials(
    firm: Firm, flagged: bool
) -> None:
    if not flagged:
        firm.tenant.features = []
        firm.tenant.save(update_fields=["features"])
    session = firm.as_accountant() if flagged else firm.as_owner()
    before = _snapshot(firm)

    response = session.post(reverse("playbook-list"), _data())

    assert response.status_code == (
        HTTPStatus.FORBIDDEN if flagged else HTTPStatus.NOT_FOUND
    )
    assert _snapshot(firm) == before
