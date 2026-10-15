"""Publication and retirement conflicts through real authenticated HTTP requests."""

from html.parser import HTMLParser
from http import HTTPStatus

import pytest
from django.urls import reverse

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles import views
from apps.cycles.models import Playbook, PlaybookVersion, VersionState
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from tests.support_threads import run_in_thread_as_runtime
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


class HiddenInputs(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.values: dict[str, str] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("type") == "hidden":
            name = attributes.get("name")
            if name:
                self.values[name] = attributes.get("value") or ""


def _step(key: str) -> StepSpec:
    return StepSpec(
        key=key,
        kind="task",
        label_ptbr=key,
        due_rule={"kind": "period_end_offset", "days": 5},
    )


@pytest.fixture
def draft() -> tuple[Firm, Playbook]:
    firm = make_firm("http-conflicts")
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    playbook = playbooks.create_playbook(
        actor=firm.owner,
        tenant=firm.tenant,
        key="http-conflicts",
        name="HTTP conflicts",
        steps=[_step("a"), _step("b")],
    )
    return firm, playbook


def _snapshot(playbook: Playbook) -> tuple[object, ...]:
    """Include every domain column and the firm's append-only event identities."""
    return (
        Playbook.objects.filter(pk=playbook.pk).values().get(),
        playbook.versions.values().get(),
        list(playbook.versions.get().steps.order_by("position").values()),
        list(Event.objects.order_by("pk").values_list("pk", flat=True)),
    )


@pytest.mark.parametrize(
    ("expected_delta", "concurrent_edit"),
    [(0, True), (1, True), (1, False)],
    ids=["previewed", "unpreviewed", "mismatched-counter"],
)
def test_publish_rechecks_the_verified_preview_revision_under_the_lock(
    draft: tuple[Firm, Playbook],
    monkeypatch: pytest.MonkeyPatch,
    expected_delta: int,
    concurrent_edit: bool,
) -> None:
    firm, playbook = draft
    session = firm.as_owner()
    url = reverse("playbook-editor", kwargs={"pk": playbook.pk, "number": 1})
    data = {
        "action": "preview",
        "expected_version": "1",
        "name": playbook.name,
        "steps-TOTAL_FORMS": "2",
        "steps-INITIAL_FORMS": "2",
    }
    for index, key in enumerate(("a", "b")):
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
    preview = session.post(url, data=data)
    assert preview.status_code == HTTPStatus.OK
    hidden = HiddenInputs(preview.content.decode()).values
    revision = int(hidden["expected_version"])
    assert revision == 2
    assert hidden["preview_token"]

    real_check = views._preview_token_ok
    committed: list[tuple[object, ...]] = []

    def edit_after_preview() -> tuple[object, ...]:
        changed = playbooks.update_draft(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=revision,
            name="Concurrent edit",
            steps=[_step("changed-after-preview")],
        )
        assert changed.version == revision + 1
        return _snapshot(playbook)

    def interleaved_check(version: PlaybookVersion, token: str) -> bool:
        verified = real_check(version, token)
        assert verified
        assert not committed, "the interleaving must run exactly once"
        if concurrent_edit:
            # Another runtime connection commits after signature validation
            # but before this HTTP request acquires the aggregate lock.
            committed.append(
                run_in_thread_as_runtime(firm.tenant.pk, edit_after_preview)
            )
        else:
            # Even without an edit, the submitted counter must match the token.
            committed.append(_snapshot(playbook))
        return verified

    monkeypatch.setattr(views, "_preview_token_ok", interleaved_check)
    response = session.post(
        url,
        data={
            "action": "publish",
            "preview_token": hidden["preview_token"],
            "expected_version": str(revision + expected_delta),
        },
    )
    assert len(committed) == 1
    with tenant_context(firm.tenant.pk):
        assert _snapshot(playbook) == committed[0]
        assert playbook.versions.get().state == VersionState.DRAFT
    assert response.status_code == HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    "expected", [None, "invalid", "1"], ids=["missing", "malformed", "stale"]
)
def test_retire_conflicts_without_domain_or_audit_writes(
    draft: tuple[Firm, Playbook], expected: str | None
) -> None:
    firm, playbook = draft
    published, _ = playbooks.publish_version(
        actor=firm.owner,
        tenant=firm.tenant,
        playbook_pk=playbook.pk,
        number=1,
        expected_version=1,
    )
    assert published.version == 2
    session = firm.as_owner()
    with tenant_context(firm.tenant.pk):
        before = _snapshot(playbook)
    response = session.post(
        reverse("playbook-retire", kwargs={"pk": playbook.pk, "number": 1}),
        data={} if expected is None else {"expected_version": expected},
    )
    with tenant_context(firm.tenant.pk):
        assert _snapshot(playbook) == before
    assert response.status_code == HTTPStatus.CONFLICT


def test_retire_accepts_the_current_revision(draft: tuple[Firm, Playbook]) -> None:
    firm, playbook = draft
    published, _ = playbooks.publish_version(
        actor=firm.owner,
        tenant=firm.tenant,
        playbook_pk=playbook.pk,
        number=1,
        expected_version=1,
    )
    response = firm.as_owner().post(
        reverse("playbook-retire", kwargs={"pk": playbook.pk, "number": 1}),
        data={"expected_version": published.version},
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        retired = playbook.versions.get()
        assert retired.state == VersionState.RETIRED
        assert retired.version == published.version + 1
