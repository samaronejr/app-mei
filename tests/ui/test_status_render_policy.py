import ast
import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import pytest
from django.db import models
from django.template import Context, Template
from django.template.loader import render_to_string

from apps.core.templatetags import ptbr
from apps.core.tenancy import tenant_context
from apps.obligations.coverage import CoverageItem
from apps.obligations.models import SourceObservation
from apps.obligations.presentation import payment_state
from tests.cycles import observation_fixtures
from tests.cycles.observation_fixtures import payment_claim, payment_subject
from tests.portal.request_fixtures import PortalSetup

receipt_case = observation_fixtures.receipt_case

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
TEMPLATES_ROOT: Final[Path] = PROJECT_ROOT / "templates"
PTBR_SOURCE: Final[str] = inspect.getsource(ptbr)
COMMENT_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
    re.DOTALL,
)


@dataclass(frozen=True)
class EnumPolicy:
    map_name: str
    enum_name: str
    enum_class: type[models.TextChoices]
    filter_name: str
    labels: Mapping[str, object]


@dataclass(frozen=True)
class RenderSite:
    maps: frozenset[str]
    forbidden_helpers: frozenset[str]


STATUS_RENDER_SITES: Final[dict[str, RenderSite]] = {
    # T22 BillingStatus is declared with pt-BR labels, so its native display helper
    # needs no legacy translation map. This permits only this explicit render site.
    "tenants/account.html": RenderSite(frozenset(), frozenset()),
    "accounts/invite_confirm.html": RenderSite(
        frozenset({"TENANT_ROLE_LABELS"}),
        frozenset({"get_role_display"}),
    ),
    "accounts/team.html": RenderSite(
        frozenset({"TENANT_ROLE_LABELS"}),
        frozenset({"get_role_display"}),
    ),
    "clients/_identity.html": RenderSite(
        frozenset({"CLIENT_STATUS_LABELS", "GOVBR_TRUST_LEVEL_LABELS"}),
        frozenset({"get_govbr_trust_level_display", "get_status_display"}),
    ),
    "clients/detail.html": RenderSite(
        frozenset(
            {
                "OBLIGATION_STATUS_LABELS",
                "ONBOARDING_STATUS_LABELS",
                "TENANT_ROLE_LABELS",
            }
        ),
        frozenset({"get_role_display", "get_status_display"}),
    ),
    "clients/list.html": RenderSite(
        frozenset({"CLIENT_STATUS_LABELS"}),
        frozenset({"get_status_display"}),
    ),
    "cycles/workspace.html": RenderSite(
        frozenset({"ONBOARDING_STATUS_LABELS"}),
        frozenset({"get_status_display"}),
    ),
    "core/dashboard.html": RenderSite(
        frozenset({"CLIENT_STATUS_LABELS"}),
        frozenset({"get_status_display"}),
    ),
    "obligations/_rows_obligations.html": RenderSite(
        frozenset({"OBLIGATION_STATUS_LABELS"}),
        frozenset({"get_status_display"}),
    ),
    "obligations/_rows_onboarding.html": RenderSite(
        frozenset({"ONBOARDING_STATUS_LABELS"}),
        frozenset({"get_status_display"}),
    ),
    "obligations/queue.html": RenderSite(
        frozenset({"OBLIGATION_STATUS_LABELS"}),
        frozenset({"get_status_display"}),
    ),
    # T17: the Cobertura badge renders the same states the portal receipt
    # label does, through the shared component — the partial registers its own
    # includes so a hand-written status class can never sneak back in.
    "partials/cycles/coverage_item.html": RenderSite(
        frozenset(),
        frozenset({"get_status_display"}),
    ),
    "portal/partials/payment_observation.html": RenderSite(
        frozenset(),
        frozenset({"get_status_display"}),
    ),
}


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _map_enum_names() -> dict[str, str]:
    tree = ast.parse(PTBR_SOURCE)
    annotated_maps = {name for name in ptbr.__annotations__ if name.endswith("_LABELS")}
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in annotated_maps or not isinstance(node.value, ast.Dict):
            continue
        roots = {
            root
            for key in node.value.keys
            if key is not None
            if (root := _root_name(key)) is not None
        }
        assert len(roots) == 1, (
            f"{node.target.id} must derive every key from one enum, found "
            f"{sorted(roots)}"
        )
        found[node.target.id] = roots.pop()
    assert set(found) == annotated_maps, (
        f"label maps without an enum derivation: {sorted(annotated_maps - set(found))}"
    )
    return found


def enum_policies() -> tuple[EnumPolicy, ...]:
    policies = []
    for map_name, enum_name in sorted(_map_enum_names().items()):
        enum_candidate = getattr(ptbr, enum_name, None)
        assert isinstance(enum_candidate, type), f"{enum_name} is not a class"
        assert issubclass(enum_candidate, models.TextChoices), (
            f"{map_name} derives from non-TextChoices {enum_name}"
        )
        labels_candidate = getattr(ptbr, map_name)
        assert isinstance(labels_candidate, Mapping), f"{map_name} is not a mapping"
        filter_names = [
            name
            for name, function in ptbr.register.filters.items()
            if map_name in inspect.getsource(function)
        ]
        assert len(filter_names) == 1, (
            f"{map_name} must feed exactly one template filter, found {filter_names}"
        )
        policies.append(
            EnumPolicy(
                map_name,
                enum_name,
                enum_candidate,
                filter_names[0],
                cast("Mapping[str, object]", labels_candidate),
            )
        )
    assert len(policies) >= 5, (
        "the enum policy derivation missed the shipped label maps"
    )
    return tuple(policies)


def _markup_only(source: str) -> str:
    return COMMENT_BLOCK.sub(lambda block: "\n" * block.group(0).count("\n"), source)


def _template_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    roots = [TEMPLATES_ROOT, *sorted((PROJECT_ROOT / "apps").glob("*/templates"))]
    for root in roots:
        for path in sorted(root.rglob("*.html")):
            name = str(path.relative_to(root))
            assert name not in sources, f"duplicate template root entry: {name}"
            sources[name] = path.read_text(encoding="utf-8")
    return sources


def _source_helper_offenders(
    name: str,
    markup: str,
    site: RenderSite | None,
    helper_universe: set[str],
) -> list[str]:
    offenders = []
    for number, line in enumerate(markup.splitlines(), 1):
        for helper in helper_universe:
            if helper not in line:
                continue
            if site is None:
                offenders.append(
                    f"{name}:{number}: unregistered status-render site uses {helper}"
                )
            elif helper in site.forbidden_helpers:
                offenders.append(f"{name}:{number}: forbidden {helper}")
    return offenders


def _site_offenders(
    sources: Mapping[str, str],
    registry: Mapping[str, RenderSite],
    policies: Sequence[EnumPolicy],
) -> list[str]:
    filter_by_map = {policy.map_name: policy.filter_name for policy in policies}
    helper_universe = {
        helper for site in registry.values() for helper in site.forbidden_helpers
    }
    discovered: dict[str, frozenset[str]] = {}
    offenders: list[str] = []
    for name, source in sources.items():
        markup = _markup_only(source)
        used_maps = frozenset(
            map_name
            for map_name, filter_name in filter_by_map.items()
            if re.search(rf"\|\s*{re.escape(filter_name)}\b", markup)
        )
        if used_maps:
            discovered[name] = used_maps
        site = registry.get(name)
        offenders.extend(_source_helper_offenders(name, markup, site, helper_universe))
    for name in sorted(set(registry) | set(discovered)):
        expected = registry.get(name)
        actual = discovered.get(name, frozenset())
        if expected is None:
            offenders.append(f"{name}: unregistered enum filters {sorted(actual)}")
        elif expected.maps != actual:
            offenders.append(
                f"{name}: registered maps {sorted(expected.maps)} but renders "
                f"{sorted(actual)}"
            )
    return sorted(offenders)


def _application_site_offenders() -> list[str]:
    sources = _template_sources()
    policies = enum_policies()
    assert len(sources) > 20, "the status-render policy found no template tree"
    assert STATUS_RENDER_SITES, "the status-render registry is empty"
    assert set(STATUS_RENDER_SITES).issubset(sources), (
        "a registered status-render template does not exist"
    )
    registered_maps = {
        map_name for site in STATUS_RENDER_SITES.values() for map_name in site.maps
    }
    policy_maps = {policy.map_name for policy in policies}
    assert registered_maps == policy_maps, (
        "enum maps without registered render sites: "
        f"{sorted(policy_maps - registered_maps)}; "
        f"unknown registered maps: {sorted(registered_maps - policy_maps)}"
    )
    return _site_offenders(sources, STATUS_RENDER_SITES, policies)


def _enum_map_offenders(policies: Sequence[EnumPolicy]) -> list[str]:
    assert policies, "the exhaustive enum helper received no derived policies"
    offenders = []
    for policy in policies:
        enum_values = {str(member.value) for member in policy.enum_class}
        map_values = set(policy.labels)
        if enum_values != map_values:
            offenders.append(
                f"{policy.map_name}: missing {sorted(enum_values - map_values)}, "
                f"unexpected {sorted(map_values - enum_values)}"
            )
    return offenders


def _render_every_enum_member(policies: Sequence[EnumPolicy]) -> list[str]:
    assert policies, "the rendered-enum helper received no derived policies"
    expressions = ["{% load ptbr %}", "<main>"]
    context: dict[str, str] = {}
    expected: list[tuple[str, str]] = []
    for policy in policies:
        members = list(policy.enum_class)
        assert members, f"{policy.enum_name} has no members to render"
        for index, member in enumerate(members):
            variable = f"value_{policy.map_name.lower()}_{index}"
            marker = f"{policy.map_name}:{member.name}"
            context[variable] = str(member.value)
            expressions.append(
                f"<span data-policy='{marker}'>"
                f"{{{{ {variable}|{policy.filter_name} }}}}</span>"
            )
            expected.append((marker, str(policy.labels[str(member.value)])))
    expressions.append("</main>")
    rendered = Template("\n".join(expressions)).render(Context(context))
    assert rendered.strip(), "the every-member render returned an empty page"
    return [
        f"{marker}: expected {label!r}"
        for marker, label in expected
        if f"data-policy='{marker}'>{label}</span>" not in rendered
    ]


def test_registered_status_render_sites_use_policy_filters() -> None:
    offenders = _application_site_offenders()

    assert offenders == [], "status-render policy violation:\n  " + "\n  ".join(
        offenders
    )


def test_every_derived_enum_map_covers_and_renders_every_member() -> None:
    policies = enum_policies()
    offenders = [*_enum_map_offenders(policies), *_render_every_enum_member(policies)]

    assert offenders == [], "enum render policy violation:\n  " + "\n  ".join(offenders)


def test_the_site_scan_reports_markup_and_blanks_comment_prose() -> None:
    policy = next(
        policy
        for policy in enum_policies()
        if policy.map_name == "OBLIGATION_STATUS_LABELS"
    )
    planted = {
        "plantado.html": """
{% comment %}
The old row.get_status_display helper was replaced.
{% endcomment %}
<td>{{ row.get_status_display }}</td>
<td>{{ row.status|situacao_obrigacao }}</td>
"""
    }
    registry = {
        "plantado.html": RenderSite(
            frozenset({policy.map_name}),
            frozenset({"get_status_display"}),
        )
    }

    offenders = _site_offenders(planted, registry, [policy])

    assert offenders == ["plantado.html:5: forbidden get_status_display"]
    assert _markup_only(planted["plantado.html"]).count("\n") == planted[
        "plantado.html"
    ].count("\n")


# ---------------------------------------------------------------------------
# T17: observation sources render through one adapter and one badge.
# The matrix below is this file's contract for three surfaces at once: the
# portal receipt partial, the staff coverage badge and the status_badge
# component itself. Mutating `staff_recorded` into the verified style — or any
# state name into a style it did not earn — must fail here, not in a snapshot.
# ---------------------------------------------------------------------------

_ADAPTER_CASES: list[tuple[str, str, str, str]] = [
    # (source_kind, verification_status, outcome, expected portal state)
    ("official_api", "verified", "accepted", "verified"),
    ("official_file_import", "verified", "accepted", "verified"),
    ("staff_recorded", "unknown", "accepted", "manual"),
    ("staff_recorded", "unknown", "recorded", "manual"),
    ("staff_recorded", "pending_review", "accepted", "manual"),
    ("client_submitted", "pending_review", "submitted", "manual"),
    ("client_submitted", "pending_review", "none", "manual"),
    ("legacy_status", "unknown", "recorded", "manual"),
    ("legacy_status", "not_applicable", "recorded", "manual"),
    ("official_api", "stale", "accepted", "stale"),
    ("official_api", "refresh_failed", "refresh_failed", "stale"),
    ("official_api", "pending_review", "accepted", "unknown"),
    ("staff_recorded", "unsupported", "rejected", "unknown"),
    ("staff_recorded", "unsupported", "retracted", "unknown"),
    # A deterministic rule can assert a schedule; it can never borrow a badge
    # that says a person or an official source verified a payment.
    ("deterministic_rule", "verified", "accepted", "unknown"),
    ("deterministic_rule", "verified", "scheduled", "unknown"),
]


def test_payment_state_adapter_never_promotes_a_manual_source() -> None:
    """Every (source, verification, outcome) maps to exactly one portal state."""
    for source, verification, outcome, expected in _ADAPTER_CASES:
        actual = payment_state(source, verification, outcome)
        assert actual == expected, (
            f"{source}/{verification}/{outcome} rendered as {actual!r}, "
            f"not {expected!r}"
        )


@pytest.mark.django_db
def test_receipt_partial_renders_every_adapter_state() -> None:
    """The portal partial honors the adapter's state and never the forbidden word."""
    for source, verification, outcome, state in _ADAPTER_CASES:
        row = SimpleNamespace(
            payment_state=state,
            payment_source=source,
            payment_verification=verification,
            payment_outcome=outcome,
        )
        html = render_to_string(
            "portal/partials/payment_observation.html", {"row": row}
        )
        assert f'data-payment-state="{state}"' in html, (
            f"{source}/{verification}/{outcome}: expected state {state} in {html!r}"
        )
        assert "confirmad" not in html.lower()


@pytest.mark.django_db
def test_coverage_badge_maps_every_state_through_the_shared_component() -> None:
    """Each coverage state reaches the badge with the mapping's own style."""
    states: list[tuple[str, str]] = [
        ("verified", "status-confirmed"),
        ("manual", "status-manual"),
        ("pending_review", "status-pending"),
        ("stale", "status-stale"),
        ("refresh_failed", "status-stale"),
        ("unknown", "status-unknown"),
        ("not_applicable", "status-unknown"),
    ]
    for state, marker in states:
        item = SimpleNamespace(
            engagement_id="00000000-0000-0000-0000-000000000001",
            service_kind="mei_mensal",
            state=state,
            obligation_id=None,
            facets={
                "calendar": "unknown",
                "guide": "unknown",
                "payment": "unknown",
            },
            correctable=False,
        )
        html = render_to_string(
            "partials/cycles/coverage_item.html",
            {"item": item, "may_operate": False},
        )
        assert marker in html, f"{state} did not render {marker} in {html!r}"
    # unknown_scope renders as plain text: no badge may borrow a state it
    # never evaluated.
    unknown_scope = SimpleNamespace(
        engagement_id="00000000-0000-0000-0000-000000000002",
        service_kind="folha",
        state="unknown_scope",
        obligation_id=None,
        facets={"calendar": "unknown", "guide": "unknown", "payment": "unknown"},
        correctable=False,
    )
    html = render_to_string(
        "partials/cycles/coverage_item.html",
        {"item": unknown_scope, "may_operate": False},
    )
    assert "status-confirmed" not in html
    assert "status-manual" not in html


class ClaimMarkup(HTMLParser):
    """Inspect rendered semantic text and classes, not adapter constants."""

    def __init__(self) -> None:
        super().__init__()
        self.positive_classes: set[str] = set()
        self.affirmations: list[str] = []
        self.raw_states: list[str] = []
        self.times: list[datetime] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""
        self.positive_classes.update(
            set(classes.split()) & {"status-confirmed", "badge-ok"}
        )
        if tag == "time" and (value := dict(attrs).get("datetime")):
            self.times.append(datetime.fromisoformat(value))

    def handle_data(self, data: str) -> None:
        text = data.strip().lower()
        if re.match(r"^(?:pagamento )?(?:confirmad[oa]|verificad[oa])\b", text):
            self.affirmations.append(text)
        if text in {
            "verified",
            "unknown",
            "pending_review",
            "refresh_failed",
            "manual",
        }:
            self.raw_states.append(text)


def _assert_unverified_markup(html: str) -> None:
    parsed = ClaimMarkup()
    parsed.feed(html)
    assert not parsed.positive_classes, parsed.positive_classes
    assert not parsed.affirmations, parsed.affirmations
    assert not parsed.raw_states, parsed.raw_states


def _populated_surfaces(setup: PortalSetup) -> dict[str, str]:
    firm = setup.firm()
    surfaces = {}
    for name, url in (
        ("workspace", f"/clientes/{setup.graph.client.pk}/?periodo=2026-09"),
        ("cycle", f"/ciclos/{setup.cycle.pk}/"),
    ):
        response = firm.get(url)
        assert response.status_code == 200
        body = response.content.decode()
        assert 'id="cobertura"' in body
        panel = body.split('id="cobertura"', 1)[1].split("</section>", 1)[0]
        assert "data-coverage-item" in panel
        surfaces[name] = panel
    portal = setup.portal().get("/pagamentos/")
    assert portal.status_code == 200
    surfaces["portal"] = portal.content.decode()
    return surfaces


def test_source_scanner_includes_the_portal_template_root() -> None:
    sources = _template_sources()

    assert "partials/cycles/coverage_item.html" in sources
    assert "portal/partials/payment_observation.html" in sources
    planted = {
        **sources,
        "portal/repair-planted.html": "<span>{{ row.get_status_display }}</span>",
    }
    offenders = _site_offenders(planted, STATUS_RENDER_SITES, enum_policies())
    assert any("portal/repair-planted.html" in offender for offender in offenders)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "source",
    ["staff_recorded", "client_submitted", "legacy_status", "deterministic_rule"],
)
def test_populated_manual_sources_never_render_positive_claims(
    receipt_case: PortalSetup, source: str
) -> None:
    payment_claim(
        payment_subject(receipt_case),
        source=source,
        verification="verified",
        outcome="accepted",
    )

    for html in _populated_surfaces(receipt_case).values():
        _assert_unverified_markup(html)


@pytest.mark.django_db(transaction=True)
def test_populated_coverage_renders_the_actual_source_reference_and_clock(
    receipt_case: PortalSetup,
) -> None:
    subject = payment_subject(receipt_case)
    observed = datetime(2026, 9, 20, 16, 10, tzinfo=UTC)
    reference = '<script id="t17-reference">untrusted</script>'
    with tenant_context(subject.tenant_id):
        fact = SourceObservation.objects.create(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_ref=subject,
            kind="payment",
            period=subject.competence_month,
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=observed,
            source_ref=reference,
        )

    surfaces = _populated_surfaces(receipt_case)

    for name in ("workspace", "cycle"):
        html = surfaces[name]
        assert f'data-observation-id="{fact.pk}"' in html
        parsed = ClaimMarkup()
        parsed.feed(html)
        assert observed in parsed.times
        assert 'data-source-kind="staff_recorded"' in html
        assert '<script id="t17-reference">' not in html
        assert "&lt;script" in html
        _assert_unverified_markup(html)


@pytest.mark.django_db(transaction=True)
def test_populated_official_source_is_a_positive_style_control(
    receipt_case: PortalSetup,
) -> None:
    payment_claim(
        payment_subject(receipt_case),
        source="official_api",
        verification="verified",
        outcome="accepted",
    )

    for html in _populated_surfaces(receipt_case).values():
        parsed = ClaimMarkup()
        parsed.feed(html)
        assert parsed.positive_classes
        assert parsed.affirmations


@pytest.mark.django_db(transaction=True)
def test_populated_guard_detects_a_manual_only_positive_style_injection(
    receipt_case: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment_claim(payment_subject(receipt_case))
    for html in _populated_surfaces(receipt_case).values():
        _assert_unverified_markup(html)
    original = Template.render

    def inject(template: Template, context: Context) -> str:
        html = original(template, context)
        item = context.get("item")
        if (
            template.name == "partials/cycles/coverage_item.html"
            and isinstance(item, CoverageItem)
            and item.state == "manual"
        ):
            return html + '<span class="status-confirmed badge-ok">Confirmado</span>'
        return html

    monkeypatch.setattr(Template, "render", inject)
    surfaces = _populated_surfaces(receipt_case)
    for name in ("workspace", "cycle"):
        with pytest.raises(AssertionError):
            _assert_unverified_markup(surfaces[name])
