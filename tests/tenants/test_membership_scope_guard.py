"""Every firm-side Membership query must say which side of the firm it means.

`Membership` now carries a nullable `client`. A row with `client IS NULL` is firm-side;
a row with a client is a portal identity. Every pre-existing query was written when only
the first kind existed, so each one silently widened the moment the column arrived.

The most dangerous of them is the tenant middleware's `.exists()` check: a portal user
browsing the FIRM host would satisfy it, receive `app.tenant_id`, and run as
`app_runtime` — for whom no restrictive portal policy applies — reading the firm's
entire client base. That is the hole the portal isolation layer exists to close, and it
would have been re-opened from the application layer.

Three earlier versions of this guard were blind to something:

* Matching only `.filter(` missed the two `.for_user(` call sites.
* Matching `Membership\\.` case-sensitively missed `membership.objects.filter(` — the
  `django_apps.get_model(...)` plus lowercase-local idiom, used at two call sites.
* Naming a verb allow-list saw one of nineteen realistic idioms. `.get(`, `.create(`,
  `.exists(` and `.objects.select_related(...).filter(...)` all went unseen, as did
  every related-name access. Nothing was missed on the day, but the guard exists to
  catch the NEXT call site, and it could not have.

A fourth weakness was worse, because it made the escape hatch decorative rather than
merely narrow: accepting the bare substring `"client"` anywhere near the call meant a
comment containing the word satisfied the check. Deleting the `CLIENT_SCOPE_OK` marker
from `apps/accounts/mfa.py` left the guard green. It now requires a real keyword.

So the pattern matches the MANAGER rather than the verb, the approval requires an
argument rather than prose, and the module proves it can both see the real sites and
reject an unscoped one — the negative case every earlier version lacked.
"""

import re
from pathlib import Path
from typing import Final

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# No verb allow-list. An earlier version named the four verbs the codebase happened to
# call, which made it blind to `.get(`, `.create(`, `.exists(`, and to
# `.objects.select_related(...).filter(...)` — all ordinary idioms the NEXT call site is
# as likely to use as the ones already here. Matching the manager instead of the verb
# means the guard sees a query shape it has never been shown.
MEMBERSHIP_QUERY: Final = re.compile(
    r"\b(?:membership|membership_model)\.(?:objects|_default_manager|_base_manager)\."
    r"|\.(?:memberships|portal_memberships)\.",
    re.IGNORECASE,
)

# An actual keyword, not the word "client" in a comment. The bare-substring version of
# this check was satisfied by prose: deleting the marker below from apps/accounts/mfa.py
# left the guard green, because the explanatory comment above the call says "client".
CLIENT_KEYWORD: Final = re.compile(r"\bclient(?:_id)?(?:__\w+)*\s*=")

APPROVAL: Final = "# CLIENT_SCOPE_OK:"

# apps/authz resolves the caller's role and is the one place that must see BOTH kinds:
# role_of() filters on the current client context, so naming `client` there would be
# describing the wrong thing.
EXEMPT_DIRS: Final[tuple[str, ...]] = ("apps/authz",)

# A site this guard must be able to see, named explicitly so the pattern is proven
# against the idiom that defeated the previous two versions.
LOWERCASE_IDIOM_SITE: Final = "apps/core/access.py"

MINIMUM_EXPECTED_SITES: Final = 5


def _sources() -> list[Path]:
    return [
        path
        for path in (PROJECT_ROOT / "apps").rglob("*.py")
        if "__pycache__" not in path.parts
        and not any(
            str(path.relative_to(PROJECT_ROOT)).startswith(d) for d in EXEMPT_DIRS
        )
    ]


def _call_block(lines: list[str], start: int) -> str:
    """Return the statement plus the comment block above it."""
    above = []
    index = start - 1
    while index >= 0 and lines[index].lstrip().startswith("#"):
        above.append(lines[index])
        index -= 1
    below = lines[start : start + 8]
    return "\n".join([*above, *below])


def _call_sites() -> list[tuple[Path, int, str]]:
    found = []
    for path in _sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if MEMBERSHIP_QUERY.search(line):
                found.append((path, number + 1, _call_block(lines, number)))
    return found


def test_the_scan_reaches_the_codebase() -> None:
    # Given the project tree
    sources = _sources()

    # When it is enumerated
    # Then real files are found. A broken glob would make the guard below pass by
    # scanning nothing at all.
    assert len(sources) > 10
    assert any(path.name == "access.py" for path in sources)


def test_the_pattern_sees_the_lowercase_get_model_idiom() -> None:
    # Given the call site that defeated the previous two versions of this guard
    target = PROJECT_ROOT / LOWERCASE_IDIOM_SITE
    body = target.read_text(encoding="utf-8")

    # When the pattern is applied
    # Then it matches. `membership.objects.filter(` comes from django_apps.get_model,
    # so a case-sensitive `Membership\.` pattern reports a clean tree while this exact
    # site is unguarded.
    assert MEMBERSHIP_QUERY.search(body) is not None


@pytest.mark.parametrize(
    "idiom",
    [
        "Membership.objects.filter(user=u)",
        "Membership.objects.get(user=u)",
        "Membership.objects.create(user=u)",
        "Membership.objects.get_or_create(user=u)",
        "Membership.objects.update_or_create(user=u)",
        "Membership.objects.exclude(user=u)",
        "Membership.objects.for_user(u)",
        "Membership.objects.exists()",
        "Membership.objects.all()",
        "Membership.objects.count()",
        "Membership.objects.values_list('id', flat=True)",
        'Membership.objects.select_related("user").filter(tenant=t)',
        "membership.objects.filter(user=u)",
        "membership_model.objects.filter(user=u)",
        "Membership._default_manager.filter(user=u)",
        "Membership._base_manager.filter(user=u)",
        "user.memberships.filter(tenant=t)",
        "tenant.memberships.all()",
        "client.portal_memberships.all()",
    ],
)
def test_the_pattern_sees_every_realistic_query_idiom(idiom: str) -> None:
    # Given a way of reaching Membership rows that a future call site might use
    # When the pattern is applied
    # Then it matches. The previous version named four verbs and saw ONE of these,
    # so it would have reported a clean tree while `.get(` went unscoped.
    assert MEMBERSHIP_QUERY.search(idiom) is not None, idiom


def test_the_scan_finds_the_real_call_sites() -> None:
    # Given the project tree
    found = _call_sites()

    # Then several are found, so the assertion below is not passing vacuously
    assert len(found) >= MINIMUM_EXPECTED_SITES, [
        str(path.relative_to(PROJECT_ROOT)) for path, _, _ in found
    ]


def test_every_membership_query_declares_its_client_scope() -> None:
    # Given every Membership query outside apps/authz
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}:{number}"
        for path, number, block in _call_sites()
        if not CLIENT_KEYWORD.search(block) and APPROVAL not in block
    ]

    # Then each either filters on client or carries an explicit approval marker
    assert not offenders, (
        "Membership queries that do not say which side of the firm they mean.\n"
        "Add client__isnull=True for firm-side, or '"
        + APPROVAL
        + " <reason>' if both kinds are intended:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "site",
    [
        "apps/tenants/middleware.py",
        "apps/core/access.py",
        "apps/accounts/invites.py",
        "apps/clients/models/assignment.py",
        "apps/accounts/views.py",
        "apps/obligations/views.py",
    ],
)
def test_each_known_firm_side_site_filters_on_client(site: str) -> None:
    # Given a call site this change had to fix
    blocks = [
        block
        for path, _, block in _call_sites()
        if str(path.relative_to(PROJECT_ROOT)) == site
    ]

    # Then it exists and names client as a KEYWORD. Listing them individually means
    # deleting a filter fails by name rather than shrinking a count nobody checks.
    assert blocks, f"{site} no longer contains a Membership query"
    assert any(CLIENT_KEYWORD.search(block) for block in blocks), site


def test_an_unscoped_query_is_actually_flagged() -> None:
    # Given a call site that reaches Membership rows without saying which kind
    unscoped = "Membership.objects.get(user=user, is_active=True)"

    # Then the pattern sees it and nothing excuses it. This is the falsification the
    # module lacked: every other test here asserts the tree is CLEAN, which a guard
    # that can never fire also satisfies.
    assert MEMBERSHIP_QUERY.search(unscoped) is not None
    assert not CLIENT_KEYWORD.search(unscoped)
    assert APPROVAL not in unscoped


def test_prose_alone_does_not_excuse_an_unscoped_query() -> None:
    # Given a query whose surrounding comment merely mentions the word client
    prose_only = (
        "# every client of the firm is reachable from here\n"
        "Membership.objects.filter(user=user)"
    )

    # Then it is still an offender. The bare-substring rule this replaced was
    # satisfied by exactly this, which made the CLIENT_SCOPE_OK marker decorative.
    assert not CLIENT_KEYWORD.search(prose_only)
    assert APPROVAL not in prose_only

    # And the two things that DO excuse it still work
    assert CLIENT_KEYWORD.search("Membership.objects.filter(client__isnull=True)")
    assert APPROVAL in f"{APPROVAL} both kinds intended\nMembership.objects.filter(u)"
