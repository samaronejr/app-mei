"""AXR1-040: which forms carry the unsaved-changes guard, read from the render.

The guard is `x-data="unsavedGuard"` on a `<form>` (static/js/app.js); a form the
server refused also carries `data-unsaved`, so the guard starts armed. These tests
pin the inventory -- exactly the forms that edit a client's setup or a playbook
definition, armed in exactly the states where an edit can still be lost -- and its
absence where the component is not registered: the MEI-era shell loads Alpine
before app.js, so an import-only firm's pages must carry no guard at all.

The prompt itself is driven in a browser by tests/browser/test_unsaved_guard.py.
"""

from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from http import HTTPStatus
from typing import Final
from uuid import UUID

import pytest
from django.urls import reverse

from apps.clients.maintenance import (
    ContactData,
    EngagementData,
    save_contact,
    save_engagement,
)
from apps.clients.models import ClientCompany, ServiceKind
from apps.core.tenancy import tenant_context
from apps.cycles.models import Playbook, VersionState
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from tests.ui.factories import Firm, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

GUARD: Final = "unsavedGuard"
BOTH_FLAGS: Final = ("cycles_r1", "import_r1")
SECTIONS: Final = (
    "client-engagement-create",
    "client-tax-profile",
    "client-contact-create",
    "client-assignment-add",
)
STEP_KEYS: Final = ("a", "b")


@dataclass
class _Form:
    action: str
    form_id: str
    guarded: bool
    armed: bool
    intent: str = ""


class _Forms(HTMLParser):
    """Every `<form>` on a page: where it posts, its guard, and its hidden `action`."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self._open: _Form | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a form, or read the hidden `action` input of the open one."""
        attributes = dict(attrs)
        if tag == "form":
            self._open = _Form(
                action=attributes.get("action") or "",
                form_id=attributes.get("id") or "",
                guarded=attributes.get("x-data") == GUARD,
                armed="data-unsaved" in attributes,
            )
            self.forms.append(self._open)
        elif tag == "input" and self._open and attributes.get("name") == "action":
            self._open.intent = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        """Close the open form."""
        if tag == "form":
            self._open = None


def _forms(body: bytes) -> list[_Form]:
    forms = _Forms(body.decode()).forms
    assert forms, "the page rendered no form at all"
    return forms


def _guarded(body: bytes) -> dict[str, bool]:
    """Name each guarded form -- action, else id, else hidden intent -- to armed."""
    named: dict[str, bool] = {}
    for form in _forms(body):
        if form.guarded:
            name = form.action or form.form_id or form.intent or "self"
            assert name not in named, f"two guarded forms answer to {name!r}"
            named[name] = form.armed
    return named


def _firm(slug: str, flags: tuple[str, ...] = BOTH_FLAGS) -> Firm:
    firm = make_firm(slug, client_count=1)
    firm.tenant.features = list(flags)
    firm.tenant.save(update_fields=["features"])
    return firm


def _contact(firm: Firm) -> UUID:
    with tenant_context(firm.tenant.id):
        contact = save_contact(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=firm.clients[0].pk,
            data=ContactData(name="Dona Maria", email="dona.maria@padaria.example"),
        )
    return contact.pk


def _sections(client: ClientCompany) -> dict[str, bool]:
    return {reverse(name, args=[client.pk]): False for name in SECTIONS}


def test_the_setup_page_guards_its_four_editing_forms_and_nothing_else() -> None:
    firm = _firm("t40s")
    client = firm.clients[0]
    contact_id = _contact(firm)
    assignment = assign(firm, client, firm.accountant)

    page = firm.as_owner().get(reverse("client-setup", args=[client.pk]))

    assert page.status_code == HTTPStatus.OK
    assert _guarded(page.content) == _sections(client)
    unguarded = {form.action for form in _forms(page.content) if not form.guarded}
    assert reverse("client-contact-invite", args=[client.pk, contact_id]) in unguarded
    assert (
        reverse("client-assignment-remove", args=[client.pk, assignment.pk])
        in unguarded
    )


@pytest.mark.parametrize(
    ("section", "refused"),
    [
        ("client-engagement-create", {"eng-service_kind": ""}),
        ("client-tax-profile", {"tax-regime": ""}),
        ("client-contact-create", {"contato-name": ""}),
        ("client-assignment-add", {"resp-user": ""}),
    ],
)
def test_a_refused_section_comes_back_armed_and_only_that_one(
    section: str,
    refused: dict[str, str],
) -> None:
    firm = _firm("t40a")
    client = firm.clients[0]

    response = firm.as_owner().post(reverse(section, args=[client.pk]), refused)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    expected = _sections(client)
    expected[reverse(section, args=[client.pk])] = True
    assert _guarded(response.content) == expected


def test_an_import_only_firm_carries_no_guard_its_shell_cannot_run() -> None:
    firm = _firm("t40m", flags=("import_r1",))
    client = firm.clients[0]
    session = firm.as_owner()

    for url in (
        reverse("client-setup", args=[client.pk]),
        reverse("client-edit", args=[client.pk]),
        reverse("client-create"),
    ):
        page = session.get(url)
        assert page.status_code == HTTPStatus.OK, url
        assert _guarded(page.content) == {}, url
    refused = session.post(
        reverse("client-contact-create", args=[client.pk]),
        {"contato-name": ""},
    )
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert _guarded(refused.content) == {}


def test_the_client_and_row_editors_are_armed_only_when_refused() -> None:
    firm = _firm("t40f")
    client = firm.clients[0]
    contact_id = _contact(firm)
    with tenant_context(firm.tenant.id):
        engagement = save_engagement(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            data=EngagementData(
                service_kind=ServiceKind.MEI_MENSAL,
                starts_on=date(2026, 1, 1),
            ),
        )
    session = firm.as_owner()
    editors = {
        reverse("client-create"): {"cnpj": "123", "legal_name": ""},
        reverse("client-edit", args=[client.pk]): {
            "legal_name": "",
            "status": "active",
            "version": "1",
        },
        reverse("client-contact-edit", args=[client.pk, contact_id]): {
            "contato-name": "",
        },
        reverse("client-engagement-edit", args=[client.pk, engagement.pk]): {
            "eng-service_kind": "",
        },
    }

    for url, refused in editors.items():
        assert _guarded(session.get(url).content) == {"self": False}, url
        response = session.post(url, refused)
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, url
        assert _guarded(response.content) == {"self": True}, url


def test_a_stale_save_lands_on_the_conflict_page_with_nothing_armed() -> None:
    firm = _firm("t40v")
    session = firm.as_owner()
    url = reverse("client-edit", args=[firm.clients[0].pk])
    edit = {"legal_name": "Padaria Nova Ltda", "status": "active", "version": "1"}

    assert session.post(url, edit).status_code == HTTPStatus.FOUND
    stale = session.post(url, edit)

    assert stale.status_code == HTTPStatus.CONFLICT
    assert _guarded(stale.content) == {}


def _step(key: str) -> StepSpec:
    return StepSpec(
        key=key,
        kind="task",
        label_ptbr=key,
        due_rule={"kind": "period_end_offset", "days": 5},
    )


def _editor_post(
    action: str,
    *,
    expected: int,
    changes: dict[str, str] | None = None,
) -> dict[str, str]:
    """The step form's own POST, as the editor renders it for two steps."""
    data = {
        "action": action,
        "expected_version": str(expected),
        "name": "Rotina",
        "steps-TOTAL_FORMS": str(len(STEP_KEYS)),
        "steps-INITIAL_FORMS": str(len(STEP_KEYS)),
    }
    for index, key in enumerate(STEP_KEYS):
        data.update(
            {
                f"steps-{index}-key": key,
                f"steps-{index}-label_ptbr": key,
                f"steps-{index}-kind": "task",
                f"steps-{index}-owner_rule": "team",
                f"steps-{index}-due_kind": "period_end_offset",
                f"steps-{index}-due_day": "5",
            }
        )
    data.update(changes or {})
    return data


def test_only_a_draft_step_form_is_guarded_and_only_its_refusal_arms_it() -> None:
    firm = _firm("t40d", flags=("cycles_r1",))
    playbook = playbooks.create_playbook(
        actor=firm.owner,
        tenant=firm.tenant,
        key="t40-rotina",
        name="Rotina",
        steps=[_step(key) for key in STEP_KEYS],
    )
    session = firm.as_owner()
    url = reverse("playbook-editor", kwargs={"pk": playbook.pk, "number": 1})

    fresh = session.get(url)
    assert _guarded(fresh.content) == {"steps-form": False}
    assert "move" in {form.intent for form in _forms(fresh.content)}

    refused = session.post(
        url,
        _editor_post("save", expected=1, changes={"steps-0-label_ptbr": ""}),
    )
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert _guarded(refused.content) == {"steps-form": True}

    stale_move = session.post(
        url,
        {"action": "move", "step": "a", "direction": "down", "expected_version": "99"},
    )
    assert stale_move.status_code == HTTPStatus.CONFLICT
    assert _guarded(stale_move.content) == {"steps-form": False}

    previewed = session.post(url, _editor_post("preview", expected=1))
    assert previewed.status_code == HTTPStatus.OK
    assert _guarded(previewed.content) == {"steps-form": False}
    assert "publish" in {form.intent for form in _forms(previewed.content)}


def test_a_published_version_and_the_list_actions_carry_no_guard() -> None:
    firm = _firm("t40n", flags=("cycles_r1",))
    session = firm.as_owner()
    with tenant_context(firm.tenant.id):
        system = Playbook.objects.get(key="mei-mensal")
        number = system.versions.get(state=VersionState.PUBLISHED).number

    published = session.get(
        reverse("playbook-editor", kwargs={"pk": system.pk, "number": number}),
    )
    assert published.status_code == HTTPStatus.OK
    assert _guarded(published.content) == {}

    listing = session.get(reverse("playbook-list"))
    assert _guarded(listing.content) == {"create": False}
    assert "clone" in {form.intent for form in _forms(listing.content)}
