"""Four form pages answer a rejection the same way, and every POST form is armed.

Three legs here are behavioural. An invalid submission must come back carrying a
`.form-errors` summary that is announced and linked, and with the field that failed
marked invalid and tied to its own error text.
`templates/partials/pure/form_errors.html` and `field.html` already emit exactly that
markup, and nothing includes them yet — the pages below still hand the whole form to
`{{ form.as_p }}` or hand-roll it, so there is no summary to announce and no anchor to
jump to. Those legs are what turns the committed partials into a contract.

The two legs that post and the two that read source cover deliberately different sets:
see the comment above `CASES` for the one page that can be scanned but not posted to.

The fourth leg is structural, and it is the reason this file scans as well as posts.
`CsrfViewMiddleware` is installed (`config/settings/base.py`), but Django's test client
runs with `enforce_csrf_checks=False`, so every POST in this suite is accepted whether
or not the template it came from rendered a token. A `{% csrf_token %}` deleted from a
form is therefore invisible to every behavioural test in this project — the view still
answers 200, the row is still written, the assertion still passes — and visible only to
a scan of the template source. That leg quantifies over EVERY loader-reachable template
rather than the four named below, so a form added by a later wave is covered the moment
it lands rather than when someone remembers to extend a tuple.

It passes on the day it is written, because all five POST forms currently carry a token.
That is what a regression guard looks like before the regression; the red in this file
comes from the three legs above it.

Every scan enters through a helper that refuses to hand back an empty set. A glob that
matches nothing satisfies `assert not offenders` without ever reading a file, which is
the quietest way for a guard like this to stop working.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

from apps.accounts.models import User
from apps.audit.models import DataSubjectRelationship, DataSubjectRequestType
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.support import enrol_totp
from tests.ui.factories import PASSWORD
from tests.ui.templates_scan import loaded_templates

# Only the three behavioural legs need a database. The two source scans are marked
# individually rather than through a module-level `pytestmark`, so a template guard
# does not pay for a truncate-and-reseed cycle to read a file off disk.
DATABASE: Final = pytest.mark.django_db(transaction=True)

# The loader root, not BASE_DIR: a path relative to it is the template *name* the
# engine resolves, so a scanned file can never drift from the one that renders.
TEMPLATE_ROOT: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])

TEAM: Final = "accounts/team.html"
INVITE_ACCEPT: Final = "accounts/invite_accept.html"
REQUEST_FORM: Final = "lgpd/request_form.html"
SELECT_TENANT: Final = "admin/select_tenant.html"

TARGET_PAGES: Final[tuple[str, ...]] = (
    TEAM,
    INVITE_ACCEPT,
    REQUEST_FORM,
    SELECT_TENANT,
)

# `form.as_p`, never a bare `as_p`. The bare token also matches `page.has_previous`,
# which `templates/obligations/_queue.html` uses for pagination — a guard that fired
# on that would be switched off rather than fixed.
AS_P: Final = re.compile(r"form\.as_p")

# The summary container. The tag name is captured rather than assumed to be a div, so
# the guard still reads a summary an implementation chooses to render as a `<section>`.
SUMMARY: Final = re.compile(
    r"""<(\w+)\b[^>]*\bclass\s*=\s*["'][^"']*\bform-errors\b[^"']*["'][^>]*>""",
)

# Every tag that can carry a field id. `<input>` is void, the other two are not, so
# only the opening tag is captured and the accessibility attributes are read off it.
CONTROL: Final = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.IGNORECASE)

# A form that submits. Both quote styles and the unquoted form, because a token
# missing from `<form method=post>` is exactly as exploitable as one missing from the
# quoted spelling.
POST_FORM: Final = re.compile(
    r"""<form\b[^>]*\bmethod\s*=\s*["']?post\b""",
    re.IGNORECASE,
)
FORM_CLOSE: Final = "</form>"
CSRF_TOKEN: Final = re.compile(r"\{%\s*csrf_token\s*%\}")

# Stripped before the CSRF scan runs. A `<form method="post">` written inside a comment
# renders nothing and can carry no token, so counting it would make the guard fail on
# prose — and this project's templates carry a great deal of prose.
COMMENTED: Final = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}",
    re.DOTALL,
)

REDIRECTED: Final = range(300, 400)

# Mirrors `tests/audit/test_lgpd_intake.py`: a submission that is valid apart from the
# one field each case below blanks out, so exactly one field carries the error.
VALID_DSR: Final[dict[str, str]] = {
    "requester_name": "Maria Souza",
    "cpf": "529.982.247-25",
    "email": "maria@exemplo.example",
    "relationship": DataSubjectRelationship.HOLDER,
    "request_type": DataSubjectRequestType.DELETION,
    "detail": "Solicito a exclusão dos meus dados.",
}

INVITED: Final = "novo@contrato.example"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    """Serve the platform host, which is where all three posted pages live."""
    settings.ALLOWED_HOSTS = ["testserver"]


# ------------------------------------------------------------------ the four pages


@dataclass(frozen=True)
class Rejection:
    """One form page, the invalid submission it rejects, and the field that fails."""

    page: str
    submit: Callable[[], str]
    field: str

    @property
    def auto_id(self) -> str:
        """The id Django puts on the control, and `form_errors.html` links to."""
        return f"id_{self.field}"

    @property
    def error_id(self) -> str:
        """The id of the element holding this field's error text."""
        return f"{self.auto_id}_error"


def _rendered(response: "_MonkeyPatchedWSGIResponse", page: str) -> str:
    """Return the body of a rejection, refusing to read one that was redirected.

    A redirect carries no body, so every assertion beneath it would fail on an empty
    string and name the wrong thing. Naming the redirect here keeps the failure
    pointing at the view rather than at the template.
    """
    assert response.status_code not in REDIRECTED, (
        f"{page}: the rejected submission was redirected "
        f"({response.status_code}) instead of re-rendering the form with its errors"
    )
    return response.content.decode()


def _invite_accept_rejection() -> str:
    """Redeem an invite with two passwords that do not match."""
    tenant = Tenant.objects.create(name="Beta Contabilidade", slug="contrato-beta")
    _invite, raw_token = Invite.issue(
        tenant=tenant,
        email=INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    response = Client().post(
        reverse("invite-accept", args=[raw_token]),
        {
            "full_name": "Nova Pessoa",
            "password1": PASSWORD,
            "password2": f"{PASSWORD}-diferente",
        },
    )
    return _rendered(response, INVITE_ACCEPT)


def _request_form_rejection() -> str:
    """File a rights request without the document number that identifies the subject."""
    response = Client().post(reverse("dsr-submit"), {**VALID_DSR, "cpf": ""})
    return _rendered(response, REQUEST_FORM)


def _select_tenant_rejection() -> str:
    """Ask the admin selector for a firm that was never chosen."""
    tenant = Tenant.objects.create(name="Gama Contabilidade", slug="contrato-gama")
    operator = User.objects.create_user(email="op@contrato.example", is_staff=True)
    Membership.objects.create(user=operator, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(operator)
    client = Client()
    client.force_login(operator)
    response = client.post(
        reverse("admin-select-tenant"),
        {"tenant": "", "reason": "", "next": "/admin/"},
    )
    return _rendered(response, SELECT_TENANT)


# `accounts/team.html` is scanned by legs (c) and (d) and deliberately NOT posted to by
# legs (a) and (b). Its form submits to `issue_invite_view`, which answers an invalid
# submission with `render(request, REFUSAL_TEMPLATE, {"reason": ...}, status=400)` —
# `accounts/invite_refused.html`, a DIFFERENT template — rather than re-rendering the
# team page with the bound form (`apps/accounts/views.py:87-92`). There is therefore no
# `id_email` control to mark invalid and no form whose errors a summary could link to,
# and no amount of template work can produce one: the render target is a server-side
# decision this todo does not authorize changing. The refusal page is also the intended
# failure surface — todo 10 owns `invite_refused.html` and its `role="alert"` — so
# pointing these two legs at the team page would assert a contract the product does not
# have. Anyone adding `Rejection(TEAM, ...)` back needs the view changed first.
CASES: Final[tuple[Rejection, ...]] = (
    Rejection(INVITE_ACCEPT, _invite_accept_rejection, "password2"),
    Rejection(REQUEST_FORM, _request_form_rejection, "cpf"),
    Rejection(SELECT_TENANT, _select_tenant_rejection, "tenant"),
)
CASE_IDS: Final[list[str]] = [case.page for case in CASES]


# ------------------------------------------------------------------- markup readers


def _attribute(tag: str, name: str) -> str | None:
    """Return one attribute's value from a single opening tag, or None."""
    match = re.search(
        rf"""\b{re.escape(name)}\s*=\s*["']([^"']*)["']""",
        tag,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _summary(html: str) -> tuple[str, str] | None:
    """Return the error summary's opening tag and its contents, or None.

    Sliced at the first close of the *same* element name rather than at `</div>`:
    `form_errors.html` nests a `<ul>` and no second container, so the first matching
    close is this element's own, and capturing the tag name keeps that true if the
    summary is ever rendered as something other than a div.
    """
    opening = SUMMARY.search(html)
    if opening is None:
        return None
    close = html.find(f"</{opening.group(1)}>", opening.end())
    body = html[opening.end() :] if close == -1 else html[opening.end() : close]
    return opening.group(0), body


def _control(html: str, auto_id: str) -> str | None:
    """Return the opening tag of the control carrying `auto_id`, or None."""
    for match in CONTROL.finditer(html):
        if _attribute(match.group(0), "id") == auto_id:
            return match.group(0)
    return None


# ------------------------------------------------------------------ (a) the summary


@DATABASE
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_rejected_submission_comes_back_with_a_linked_error_summary(
    case: Rejection,
) -> None:
    """The summary is announced, and each entry moves focus onto the control."""
    # Given a submission this page must reject
    html = case.submit()

    # When the response is read
    found = _summary(html)

    # Then a `.form-errors` region exists, is announced the moment the page lands,
    # and links to the control that needs fixing rather than merely scrolling near it
    assert found is not None, (
        f"{case.page}: no element carrying class `form-errors` in the response to an "
        f"invalid `{case.field}`; the summary partial is not included"
    )
    opening, body = found
    assert _attribute(opening, "role") == "alert", (
        f'{case.page}: the `form-errors` summary is not `role="alert"`, so a screen '
        f"reader is never told the submission for `{case.field}` was rejected: "
        f"{opening}"
    )
    assert f'href="#{case.auto_id}"' in body, (
        f'{case.page}: the summary carries no `href="#{case.auto_id}"`, so the entry '
        f"for `{case.field}` moves focus nowhere: {body.strip()}"
    )


# -------------------------------------------------------------- (b) the failed field


@DATABASE
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_field_that_failed_is_marked_invalid_and_names_its_error(
    case: Rejection,
) -> None:
    """`aria-invalid`, the class that renders it, and the `aria-describedby` chain.

    The class is asserted alongside the two ARIA attributes rather than left to a
    styling guard, because on this page they are one mark and not two. Django 5.2
    already writes `aria-invalid="true"` and `aria-describedby="<id>_error"` from
    `BoundField.build_widget_attrs`, so `{{ form.as_p }}` satisfies both halves of the
    semantic contract on its own — and renders a control with no class at all. The
    rule that draws the error border is `.field-input[aria-invalid="true"]`, named in
    `field.html`'s own comment: without the class the attribute selects nothing, the
    field looks untouched, and a sighted person is told only that *something* failed.
    Asserting the ARIA pair alone would therefore pass today on the two `as_p` pages
    while they render exactly that.

    Every field these cases fail is a text input or a select. A checkbox carries a
    different class by design — see the branch in `field.html` — and none is one here.
    """
    # Given the same rejected submission
    html = case.submit()

    # When the control for the failing field is located
    control = _control(html, case.auto_id)

    # Then it is marked invalid both ways, and points at the text that explains it
    assert control is not None, (
        f"{case.page}: no `<input>`, `<select>` or `<textarea>` carrying "
        f'`id="{case.auto_id}"` came back for the rejected `{case.field}`'
    )
    assert _attribute(control, "aria-invalid") == "true", (
        f"{case.page}: the control for `{case.field}` does not carry "
        f'`aria-invalid="true"`, so assistive technology is never told which field '
        f"is wrong: {control}"
    )
    assert "field-input" in (_attribute(control, "class") or "").split(), (
        f"{case.page}: the control for `{case.field}` carries no `field-input` class, "
        f'so `.field-input[aria-invalid="true"]` matches nothing, no error border is '
        f"drawn, and the invalid mark is announced but never shown: {control}"
    )
    described = _attribute(control, "aria-describedby")
    assert described is not None, (
        f"{case.page}: the control for `{case.field}` carries no `aria-describedby`, "
        f"so its error text is orphaned from it: {control}"
    )
    assert case.error_id in described.split(), (
        f'{case.page}: `aria-describedby="{described}"` on `{case.field}` does not '
        f"name `{case.error_id}`"
    )
    assert f'id="{case.error_id}"' in html, (
        f"{case.page}: `{case.field}` is described by `{case.error_id}`, but no "
        f"element carries that id — the reference points at nothing, which reads to "
        f"assistive technology as broken markup"
    )


# ---------------------------------------------------------------- (c) no as_p left


def _target_source(page: str) -> str:
    """Return one target template's source, refusing to scan a file that is not there.

    The non-vacuity gate lives here rather than in a test of its own, so no scan below
    can quantify over an empty string even when run in isolation.
    """
    path = TEMPLATE_ROOT / page
    assert path.is_file(), (
        f"{page}: not found under {TEMPLATE_ROOT}; a scan of a missing file reports "
        "no offenders and passes every assertion beneath it"
    )
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{page}: empty, so scanning it proves nothing"
    return text


@pytest.mark.parametrize("page", TARGET_PAGES)
def test_no_target_page_still_hands_the_whole_form_to_as_p(page: str) -> None:
    """`as_p` writes markup no `@source` scans and no guard watches.

    It also cannot render the accessibility contract the two legs above assert as a
    whole: there is no summary, so nothing is announced and nothing is linked.
    """
    # Given the template as shipped
    text = _target_source(page)

    # When every line is read
    offenders = [
        f"{page}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if AS_P.search(line)
    ]

    # Then the form is composed from the shared field partial instead
    assert not offenders, (
        f"`form.as_p` still renders this form: {offenders}; the page cannot carry the "
        "summary or the styled control while the whole form is one opaque blob"
    )


# ------------------------------------------------------------------- (d) every token


def _post_forms() -> list[tuple[str, str]]:
    """Return `(template name, form source)` for every POST form this project ships.

    Both non-vacuity gates live here, for the same reason the file's docstring gives:
    Django's test client does not enforce CSRF, so this scan is the only thing standing
    between a deleted token and a green suite. A scan that walked no template, or found
    no form, would report no offenders and pass — which is indistinguishable from
    success and is the failure mode this helper exists to make impossible.

    A form whose `</form>` is in another template widens the window to the end of the
    file rather than being skipped. Widening can only *hide* an offender, never invent
    one, and skipping would drop a real form out of the scan entirely.
    """
    templates = list(loaded_templates())
    assert templates, (
        "no templates were discovered — this CSRF scan would report no offenders "
        "while examining nothing at all"
    )

    found: list[tuple[str, str]] = []
    for path, raw in templates:
        try:
            name = path.relative_to(TEMPLATE_ROOT).as_posix()
        except ValueError:
            name = path.as_posix()
        text = COMMENTED.sub(" ", raw)
        for opening in POST_FORM.finditer(text):
            close = text.find(FORM_CLOSE, opening.end())
            end = len(text) if close == -1 else close
            found.append((name, text[opening.start() : end]))

    assert found, (
        'no `<form method="post">` was found in any template — the CSRF guard would '
        "pass while covering nothing, and it is the only check this project has"
    )
    return found


def test_every_post_form_carries_a_csrf_token() -> None:
    """A token missing from a template is invisible to every other test here.

    `CsrfViewMiddleware` is installed, but `django.test.Client` is constructed with
    `enforce_csrf_checks=False`, so a POST from a template that renders no token is
    accepted by the suite exactly as if it had one. Nothing behavioural can see this;
    only reading the template can.
    """
    # Given every POST form in every loader-reachable template
    forms = _post_forms()

    # When each is read from its opening tag to its close
    offenders = [
        f'{name}: <form method="post"> with no {{% csrf_token %}} before </form>'
        for name, source in forms
        if not CSRF_TOKEN.search(source)
    ]

    # Then each one is armed. This covers templates later waves add, not only the four
    # form pages above, because the next unarmed form is the one nobody is looking at.
    assert not offenders, f"POST forms with no CSRF token: {offenders}"
