import ast
import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from django.db import models
from django.template import Context, Template

from apps.core.templatetags import ptbr

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
    return {
        str(path.relative_to(TEMPLATES_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATES_ROOT.rglob("*.html"))
    }


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
