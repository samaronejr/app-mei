"""The document vault's screen: what a client may see, and what they may send.

Todo 25. The list is confined by the database and by nothing the view wrote, exactly as
the payments page is. Two things about this page differ from that one DELIBERATELY, and
both are written down here because "make them agree" is the obvious wrong edit.

**The gate is not the same, and that is not an oversight.** `portal_payments` is
login-only because no payment-side capability is FULL for both client roles — an
object-less `require_can("das.generate")` would 403 a MEI owner on her own payment list.
`documents.transfer` IS FULL for both, so this page carries the same three decorators
its upload and download siblings already carry. Harmonising the two would either lock
both client roles out of payments or drop a gate the vault is entitled to.

**The rows are model instances rather than `.values()`, and that inverts the payments
reasoning on purpose.** There, flattening to scalars stops a template following
`obligation_type` onto a table `app_portal` is denied. Here, flattening would buy the
OPPOSITE of what it looks like it buys: a `.values()` row renders a forbidden traversal
as an empty string, so the page would look perfectly correct and the structural case
below would be the only thing that ever noticed. Handing the template the row makes the
same mistake `permission denied` on the spot — loud, in development, on the first render
— and the structural case is then a second line of defence rather than the only one.
This todo's QA injection asserts both halves of exactly that claim.

Every column the page draws is local to `obligations_document`. The three foreign keys
on that model each point at a table the portal role either cannot read at all or has no
business reading here, so the ban below is on the traversal rather than on the column.
"""

import ast
import hashlib
import io
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import ProgrammingError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, new_storage_key
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST: Final = "acme-portal.localhost"
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PORTAL_VIEWS: Final[Path] = PROJECT_ROOT / "apps" / "portal" / "views.py"
PORTAL_TEMPLATES: Final[Path] = (
    PROJECT_ROOT / "apps" / "portal" / "templates" / "portal"
)
DOCUMENTS_TEMPLATE: Final[Path] = PORTAL_TEMPLATES / "documents.html"
UPLOAD_ERROR_TEMPLATE: Final[Path] = PORTAL_TEMPLATES / "upload_error.html"
UPLOAD_FORM_TEMPLATE: Final[Path] = PORTAL_TEMPLATES / "partials" / "upload_form.html"

PAGE_SIZE: Final = 12

# Timestamps are stored in UTC and rendered in Brasília time, which is what
# `apps/core/templatetags/ptbr.py` exists to do. The test converts the same way rather
# than asserting the stored value, or it would pin the page to a clock nobody reads.
BRT: Final = ZoneInfo("America/Sao_Paulo")

# Alpha's own evidence. Fourteen against a twelve-row window, so the slice is asserted
# against a vault that exceeds one page rather than one that happens to fit.
ALPHA_FILENAMES: Final = tuple(f"nota-fiscal-{index:02d}.pdf" for index in range(1, 15))
ALPHA_TOTAL: Final = len(ALPHA_FILENAMES)
# Seeded LAST, so `-created_at` puts them at the very top of an UNCONFINED first page.
# That is the falsifiable half: were the RESTRICTIVE policy dropped, the confinement
# case fails on the first row rather than somewhere in a tail nobody reads.
BETA_FILENAMES: Final = ("SEGREDO-DA-BETA-01.pdf", "SEGREDO-DA-BETA-02.pdf")

# Present only on page two, so their absence from page one is what proves the paginator
# actually sliced rather than rendering the whole vault.
ALPHA_PAGE_TWO: Final = ALPHA_FILENAMES[:2]

# ------------------------------------------------------------------ the pt-BR copy

HEADING: Final = "Documentos"
UPLOAD_HEADING: Final = "Enviar documento"
TYPES_HINT: Final = "Envie PDF, JPG ou PNG."
SUBMIT_LABEL: Final = "Enviar arquivo"
DOWNLOAD_LABEL: Final = "Baixar"
EMPTY_STATE: Final = (
    "Nenhum documento enviado ainda. Envie o primeiro arquivo no formulário acima."
)
# Success AND what it means, never merely "ok". A MEI owner needs to know the upload
# reached the person who acts on it, which is the only reason she sent it.
UPLOAD_SUCCESS: Final = "Documento enviado. Sua contadora já pode vê-lo."

ERROR_HEADING: Final = "Não foi possível enviar"
NO_FILE_COPY: Final = (
    "Nenhum arquivo foi escolhido. Escolha um arquivo e envie de novo."
)
TOO_LARGE_COPY: Final = "O arquivo é maior que o limite de {label}. Envie um menor."
SIZE_HINT: Final = "Até {label} por arquivo."

# Two caps, neither of them the shipped one. The hint is asserted to MOVE when the
# setting moves, which is a claim about the page tracking the server; asserting the
# shipped figure would restate a number that already lives in settings and would pass
# against a page that hard-codes it.
SMALL_CAP: Final = 2 * 1024 * 1024
SMALL_CAP_LABEL: Final = "2,0 MB"
LARGE_CAP: Final = 5 * 1024 * 1024
LARGE_CAP_LABEL: Final = "5,0 MB"

# --------------------------------------------------------------- the query window

ROLE_SWITCH: Final = "SET LOCAL ROLE app_portal"
COMMIT: Final = "COMMIT"
DOCUMENT_TABLE: Final = "obligations_document"
USER_TABLE: Final = "accounts_user"
OBLIGATION_TABLE: Final = "obligations_obligation"

# The page's OWN cost: everything between the role switch and the matching COMMIT.
# Scoped to that window for the reason `tests/portal/test_portal_home.py` sets out at
# length — roughly nineteen statements of fixed middleware plumbing precede it, and a
# per-row query over a twelve-row page would hide inside a whole-request ceiling without
# ever breaching it. Twelve rows of model instances is precisely the shape that CAN grow
# an N+1, which is why this page has a budget at all.
QUERY_BUDGET: Final = 15

# --------------------------------------------------------------- structural bans

# Everything that narrows a queryset. `Q` sits beside `filter` and `exclude` because
# `.filter(Q(client=...))` is the same restriction wearing a different callable.
NARROWING_CALLS: Final = frozenset({"filter", "exclude", "Q"})

# Relations this page's templates must never follow, and where each one lands:
#
#   uploaded_by   obligations_document -> accounts_user. app_portal holds no SELECT on
#                 that table at all, so this is `permission denied` rather than a slow
#                 page -- and accounts_user is where the password hashes are.
#   obligation.   obligations_document -> obligations_obligation. That table IS
#                 readable, so this one does NOT fail loudly; it silently issues a
#                 second query per row inside the transaction, which is the quieter and
#                 therefore worse half of the same mistake.
#   client.       obligations_document -> clients_clientcompany, likewise readable and
#                 likewise a per-row query. The shell already draws the company.
#
# The trailing dot is part of each literal on purpose: it is the traversal that is
# banned, not the word. A view-provided scalar may be named after the thing it came
# from without reaching through anything.
FORBIDDEN_TRAVERSALS: Final = ("uploaded_by", "obligation.", "client.")

# Only what the template ENGINE evaluates: `{{ ... }}` and `{% ... %}`. Scanning the raw
# text instead is the same mistake `tests/portal/test_portal_payments.py` documents on
# the Python side and solves with `ast` -- these templates carry long prose comments
# explaining precisely which relations they do not follow, and a line scan reds on the
# sentence describing the absence of the thing it looks for. `client.` ending a
# sentence is prose; `{{ document.client.legal_name }}` is a query. Matching only
# inside an evaluated span tells the two apart, and `{% comment %}` bodies are excluded
# because nothing in them is evaluated at all.
TEMPLATE_EXPRESSION: Final = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
COMMENT_BLOCK: Final = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
    re.DOTALL,
)


def _evaluated_spans(source: str) -> list[str]:
    """Return every span the template engine will evaluate, comments removed."""
    return TEMPLATE_EXPRESSION.findall(COMMENT_BLOCK.sub("", source))


# Anything that would put a script, an inline style or an inline handler on a page the
# portal serves. The portal is the one boundary in this product that row-level security
# is enforcing, and executable content shipped across it is a shared attack surface.
SCRIPTED: Final = re.compile(r"<script\b|\bhx-|\bx-on:|\s@click\b|\bstyle=|data:")


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    # Pinned to the shipped tree: sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


@dataclass(frozen=True)
class Firm:
    """One firm, two MEI clients, and three accounts that reach the portal differently.

    Both client roles are held, because `documents.transfer` is FULL for each of them
    and the claim under test is that the database confines the two identically. The
    firm-side accountant is here to be refused at the middleware.
    """

    tenant: Tenant
    alpha: ClientCompany
    beta: ClientCompany
    owner: User
    collaborator: User
    staff: User
    alpha_key: str
    beta_key: str

    def signed_in_as(self, user: User) -> Client:
        """Return a test client already holding `user`'s session."""
        http = Client()
        http.force_login(user)
        return http


def _company(tenant: Tenant, legal_name: str, cnpj: str) -> ClientCompany:
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        return ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name=legal_name,
            cnpj=cnpj,
            is_mei=True,
        )


def _document(
    tenant: Tenant,
    client: ClientCompany,
    user: User,
    filename: str,
    *,
    store_bytes: bool = False,
) -> Document:
    """Seed one row, and its bytes only when a test is going to ask for them.

    The content is derived from the filename so that every row carries a distinct
    digest: uniqueness on `(tenant, client, sha256)` is client-scoped, and two identical
    payloads under one client would be refused by the constraint rather than by anything
    this module is testing.
    """
    payload = f"conteudo de {filename}".encode()
    document = Document(
        tenant=tenant,
        client=client,
        sha256=hashlib.sha256(payload).hexdigest(),
        original_filename=filename,
        content_type="application/pdf",
        byte_size=len(payload),
        uploaded_by=user,
        storage_key=new_storage_key(),
    )
    if store_bytes:
        default_storage.save(document.storage_key, io.BytesIO(payload))
    with tenant_context(tenant.id):
        document.save(force_insert=True)
    return document


def _accounts(tenant: Tenant, alpha: ClientCompany) -> tuple[User, User, User]:
    owner = User.objects.create_user(email="dono@mei.example", password=PASSWORD)
    helper = User.objects.create_user(email="ajuda@mei.example", password=PASSWORD)
    staff = User.objects.create_user(email="contadora@acme.example", password=PASSWORD)
    for account in (owner, helper, staff):
        enrol_totp(account)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    Membership.objects.create(
        user=helper,
        tenant=tenant,
        role=TenantRole.CLIENT_COLLABORATOR,
        client=alpha,
    )
    # Firm-side, and therefore `client=None`: a CHECK constraint enforces the pairing.
    Membership.objects.create(user=staff, tenant=tenant, role=TenantRole.OWNER)
    return owner, helper, staff


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    alpha = _company(tenant, "CLIENTE ALPHA", "11222333000181")
    beta = _company(tenant, "CLIENTE BETA", "11444777000161")
    owner, helper, staff = _accounts(tenant, alpha)

    # Seeded in name order, so `-created_at` reads them back in REVERSE name order:
    # `nota-fiscal-14.pdf` is the newest and leads page one, and the two lowest-numbered
    # are the two that fall onto page two.
    alpha_documents = [
        _document(tenant, alpha, owner, filename, store_bytes=index == 0)
        for index, filename in enumerate(ALPHA_FILENAMES)
    ]
    # Seeded AFTER alpha's, so `created_at` puts the sibling's rows newest. An
    # unconfined `-created_at` ordering therefore leads with them.
    beta_documents = [
        _document(tenant, beta, staff, filename, store_bytes=True)
        for filename in BETA_FILENAMES
    ]
    return Firm(
        tenant,
        alpha,
        beta,
        owner,
        helper,
        staff,
        alpha_key=alpha_documents[0].storage_key,
        beta_key=beta_documents[0].storage_key,
    )


@pytest.fixture
def bare_firm() -> Firm:
    """The same shape with alpha's vault empty, so the empty state is reachable.

    Beta keeps her documents. "Alpha has nothing" and "the table has nothing" are
    different claims, and only the first of them says anything about the policy.
    """
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    alpha = _company(tenant, "CLIENTE ALPHA", "11222333000181")
    beta = _company(tenant, "CLIENTE BETA", "11444777000161")
    owner, helper, staff = _accounts(tenant, alpha)

    beta_documents = [
        _document(tenant, beta, staff, filename) for filename in BETA_FILENAMES
    ]
    return Firm(
        tenant,
        alpha,
        beta,
        owner,
        helper,
        staff,
        alpha_key="",
        beta_key=beta_documents[0].storage_key,
    )


# ----------------------------------------------------------------------- the paths


def _list_path() -> str:
    """Reverse the vault page against the tree the portal host actually serves.

    `reverse()` with no urlconf resolves against `ROOT_URLCONF`, which mounts none of
    the portal names. Reversing rather than hard-coding `/documentos/` also means a
    route registered a SECOND time — which shadows silently rather than erroring — is
    exercised through whichever pattern `reverse()` resolves to.
    """
    return str(reverse("portal-documents", urlconf=PORTAL_URLCONF))


def _upload_path() -> str:
    return str(reverse("portal-document-upload", urlconf=PORTAL_URLCONF))


def _download_path(storage_key: str) -> str:
    return str(
        reverse(
            "portal-document-download",
            args=[storage_key],
            urlconf=PORTAL_URLCONF,
        ),
    )


def _documents(http: Client, query: str = "") -> str:
    """GET the vault page and return the rendered document, refusing anything else.

    The status gate is the non-vacuity control for every scan below it. A portal
    template reaching a table `app_portal` cannot read raises rather than returning a
    document, and a redirect returns an empty body that satisfies every "...is absent"
    assertion perfectly.
    """
    target = f"{_list_path()}?{query}" if query else _list_path()
    response = http.get(target, headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, (
        f"{target} answered {response.status_code}, so nothing scanned below is a "
        f"rendered page"
    )
    return str(response.content.decode())


def _upload(
    http: Client,
    content: bytes,
    name: str = "recibo-novo.pdf",
    *,
    follow: bool = False,
) -> "_MonkeyPatchedWSGIResponse":
    return http.post(
        _upload_path(),
        {"file": SimpleUploadedFile(name, content, content_type="application/pdf")},
        headers={"host": PORTAL_HOST},
        follow=follow,
    )


def _portal_window(http: Client) -> list[str]:
    """Return the statements the page itself issued: the role switch to the commit.

    Mirrors `_page_statements` in `tests/portal/test_portal_home.py` — mirrored rather
    than imported, as every guard in this package is, because importing it would drag
    that module's fixtures and its own `pytestmark` in with it. The two gates below are
    the point: a slice taken from a request that never entered portal context would be
    empty, and an empty slice satisfies any ceiling at all.
    """
    with CaptureQueriesContext(connection) as captured:
        response = http.get(_list_path(), headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, response.status_code
    statements = [row["sql"] for row in captured.captured_queries]

    role_at = next(
        (index for index, sql in enumerate(statements) if ROLE_SWITCH in sql),
        None,
    )
    assert role_at is not None, (
        f"no {ROLE_SWITCH!r} was issued, so the measured window never opened and every "
        f"assertion over it holds vacuously; the statements were {statements}"
    )
    commit_at = next(
        (
            index
            for index, sql in enumerate(statements)
            if index > role_at and sql.strip() == COMMIT
        ),
        None,
    )
    assert commit_at is not None, (
        f"the portal transaction never committed: {statements}"
    )

    inside = statements[role_at + 1 : commit_at]
    assert inside, "the page issued nothing at all between the role switch and COMMIT"
    return inside


def _portal_documents_ast() -> ast.FunctionDef:
    """Return the view's parse tree, or fail rather than scan a function that moved."""
    tree = ast.parse(PORTAL_VIEWS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "portal_documents":
            return node
    pytest.fail(f"{PORTAL_VIEWS.name} defines no portal_documents")


def _narrowing_name(call: ast.Call) -> str:
    """Name the callable, whether it is `qs.filter` or a bare `Q`."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _narrows_by_client(call: ast.Call) -> bool:
    """Report whether this call restricts a queryset by client."""
    if _narrowing_name(call) not in NARROWING_CALLS:
        return False
    return any(
        keyword.arg is not None and keyword.arg.startswith("client")
        for keyword in call.keywords
    )


# =============================================================== (a) the confinement


@pytest.mark.parametrize("role", ["owner", "collaborator"])
def test_the_vault_lists_only_this_clients_documents_for_either_role(
    firm: Firm,
    role: str,
) -> None:
    """The page filters nothing. Both client roles still see only alpha's evidence.

    Beta's rows were seeded LAST, so they are the newest in the table and an unconfined
    `order_by('-created_at')` would put them at the top of this very page. That is the
    falsifiable half: were the RESTRICTIVE policy comparing `app.client_id` dropped,
    this fails on the first row rather than in a tail nobody reads.
    """
    body = _documents(firm.signed_in_as(getattr(firm, role)))

    newest = ALPHA_FILENAMES[-1]
    assert newest in body, (
        f"{newest} is alpha's newest document and is missing from her own vault, so "
        f"the absences below prove nothing"
    )

    for filename in BETA_FILENAMES:
        assert filename not in body, (
            f"{filename} belongs to the sibling client; as {role} this page is showing "
            f"another company's fiscal evidence, which is what a dropped RESTRICTIVE "
            f"policy looks like"
        )
    assert firm.beta_key not in body, (
        "the sibling's storage key reached the page, so a download link to another "
        "client's bytes could be built straight out of the HTML"
    )
    assert firm.beta.legal_name not in body


def test_the_view_applies_no_client_filter_of_its_own() -> None:
    """The confinement above must be the database's work, not the view's.

    A `.filter(client=...)` would make the case above pass with the policy dropped,
    which is the one outcome that leaves the isolation untested while looking green.

    Read off the parse tree rather than the text, and that is not fastidiousness. The
    view's docstring explains at length why no such filter is there, and explaining it
    means SPELLING it — so a line scan reds on the prose describing the absence of the
    very thing it looks for. `ast` sees calls, and a sentence is not a call.
    """
    view = _portal_documents_ast()

    # The gate that keeps the scan honest: were the query moved into a helper, this
    # function would no longer contain the code a filter could hide in.
    assert any(
        isinstance(node, ast.Name) and node.id == "Document" for node in ast.walk(view)
    ), (
        "portal_documents issues no query of its own, so the scan below covers a "
        "function that could not contain the filter it is looking for"
    )

    offenders = [
        f"{PORTAL_VIEWS.name}:{call.lineno}: {_narrowing_name(call)}(...)"
        for call in ast.walk(view)
        if isinstance(call, ast.Call) and _narrows_by_client(call)
    ]
    assert offenders == [], (
        f"portal_documents narrows by client itself: {offenders}. The RESTRICTIVE "
        f"policy comparing app.client_id is the control, and a hand-written filter "
        f"would hide its removal behind a page that still looked correct"
    )


def test_the_page_renders_without_a_denied_read_under_the_portal_role(
    firm: Firm,
) -> None:
    """Every table this page touches is one `app_portal` holds SELECT on.

    The whole render — view and template both — happens inside the transaction and
    after `SET LOCAL ROLE app_portal`, so a read of `accounts_user` surfaces as
    `ProgrammingError: permission denied`, which the test client re-raises. The role
    switch is asserted separately, because a request that fell through the middleware
    would raise nothing and pass this vacuously.
    """
    http = firm.signed_in_as(firm.owner)
    try:
        with CaptureQueriesContext(connection) as captured:
            response = http.get(_list_path(), headers={"host": PORTAL_HOST})
    except ProgrammingError as denied:  # pragma: no cover - the failure this guards
        pytest.fail(
            f"the vault page read a table app_portal is denied: {denied}. The fix is "
            f"always the same shape — render a column the row already carries rather "
            f"than reaching through a relation to fetch one",
        )

    assert response.status_code == HTTPStatus.OK
    statements = [row["sql"] for row in captured.captured_queries]
    assert any(ROLE_SWITCH in sql for sql in statements), (
        f"the request never issued {ROLE_SWITCH!r}, so it never ran as the portal role "
        f"and 'no permission denied' is true of nothing; the statements were "
        f"{statements}"
    )


def test_a_firm_side_accountant_is_refused_on_the_portal_host(firm: Firm) -> None:
    """403, from the middleware, before the view is ever reached."""
    response = firm.signed_in_as(firm.staff).get(
        _list_path(),
        headers={"host": PORTAL_HOST},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN, (
        f"the vault answered {response.status_code} to a firm-side membership; the "
        f"portal host serves client-role accounts only"
    )


# ================================================= (b) only this table's own columns


def test_no_vault_template_reaches_through_a_relation() -> None:
    """Structural. The page renders columns local to `obligations_document`, only.

    `{{ document.uploaded_by.email }}` reads like attribute access and compiles to a
    second query during rendering — inside the transaction, under the portal role,
    against `accounts_user`, which `app_portal` holds no SELECT on and which is where
    the password hashes live. `obligation.` and `client.` land on tables the role CAN
    read, so those two do not fail loudly at all; they quietly cost a query per row.

    The view hands the template the row precisely so that the loud failure is available
    at all — see this module's docstring — which makes this case the second line of
    defence rather than the only one.
    """
    templates = [DOCUMENTS_TEMPLATE, UPLOAD_ERROR_TEMPLATE, UPLOAD_FORM_TEMPLATE]
    present = [path for path in templates if path.exists()]
    assert present, (
        f"none of {[path.name for path in templates]} exists, so this scan quantifies "
        f"over nothing and passes without inspecting a single line"
    )
    assert DOCUMENTS_TEMPLATE in present, f"{DOCUMENTS_TEMPLATE} is missing"

    spans = {
        path: _evaluated_spans(path.read_text(encoding="utf-8")) for path in present
    }
    assert any(spans.values()), (
        "no vault template evaluates anything at all, so the scan below quantifies "
        "over an empty set and passes without inspecting a single expression"
    )

    offenders = [
        f"{path.name}: {span.strip()}"
        for path, found in spans.items()
        for span in found
        for literal in FORBIDDEN_TRAVERSALS
        if literal in span
    ]
    assert offenders == [], (
        f"a vault template follows a relation while rendering: {offenders}. Render a "
        f"column the row already carries instead — and note that the word alone is "
        f"fine, it is the trailing dot that issues the query"
    )


def test_the_compiled_sql_never_reaches_the_account_table(firm: Firm) -> None:
    """The behavioural half: what the database was asked, not what was typed.

    A text scan pins the spellings somebody thought of. This reads the statements the
    page issued inside portal context and asserts none names `accounts_user` at all,
    which holds however the traversal were written.
    """
    inside = _portal_window(firm.signed_in_as(firm.owner))

    assert any(DOCUMENT_TABLE in sql for sql in inside), (
        f"no statement in the portal window touched {DOCUMENT_TABLE}, so this page "
        f"read no documents and the absence asserted below is the absence of any "
        f"query at all; the statements were {inside}"
    )
    for table in (USER_TABLE, OBLIGATION_TABLE):
        offenders = [sql for sql in inside if table in sql]
        assert offenders == [], (
            f"{table} appears in the compiled SQL of the vault page: {offenders}. "
            f"Nothing this page draws comes from it"
        )


def test_the_vault_page_stays_within_its_query_budget(firm: Firm) -> None:
    """Twelve rows, and not one of them may cost a query.

    This is the budget the decision to pass model instances buys its way out of. A page
    that reached through a foreign key once per row would issue twelve more statements
    than this ceiling allows; a page rendering what the view already fetched issues two,
    the paginator's count and the slice.
    """
    inside = _portal_window(firm.signed_in_as(firm.owner))

    assert len(inside) <= QUERY_BUDGET, (
        f"the vault page issued {len(inside)} statements between {ROLE_SWITCH!r} and "
        f"{COMMIT}, over the budget of {QUERY_BUDGET}. That window is the page's own "
        f"work, so this is something the page grew rather than something the "
        f"middleware chain did:\n  " + "\n  ".join(inside)
    )


def test_the_first_page_shows_exactly_the_page_size(firm: Firm) -> None:
    """Twelve of fourteen, so the slice is asserted against a vault that exceeds it."""
    assert ALPHA_TOTAL > PAGE_SIZE, (
        f"alpha holds {ALPHA_TOTAL} documents and the page shows {PAGE_SIZE}; the "
        f"fixture must exceed one page or pagination is never exercised"
    )
    body = _documents(firm.signed_in_as(firm.owner))

    shown = [name for name in ALPHA_FILENAMES if name in body]
    assert len(shown) == PAGE_SIZE, (
        f"the first page renders {len(shown)} documents rather than {PAGE_SIZE}"
    )
    for filename in ALPHA_PAGE_TWO:
        assert filename not in body, (
            f"{filename} belongs to page two and is on page one, so the list is not "
            f"sliced to {PAGE_SIZE} rows"
        )

    second = _documents(firm.signed_in_as(firm.owner), "pagina=2")
    for filename in ALPHA_PAGE_TWO:
        assert filename in second, f"{filename} is missing from page two"
    for filename in BETA_FILENAMES:
        assert filename not in second, (
            f"{filename} is the sibling's; paging past the first slice escaped the "
            f"confinement"
        )


def test_a_row_states_the_name_the_date_and_the_size(firm: Firm) -> None:
    """Three columns, all local to the row, plus a machine-readable timestamp.

    A vault listing filenames alone tells a client nothing about whether the thing they
    are looking at is the document they meant to send.
    """
    body = _documents(firm.signed_in_as(firm.owner))
    newest = ALPHA_FILENAMES[-1]

    with tenant_context(firm.tenant.id):
        row = Document.objects.get(original_filename=newest)

    assert newest in body
    assert f'<time datetime="{row.created_at.isoformat()}"' in body, (
        "the upload date is not wrapped in a <time> carrying an ISO datetime attribute"
    )
    assert row.created_at.astimezone(BRT).strftime("%d/%m/%Y %H:%M") in body, (
        "the upload date is never rendered in the dd/mm/aaaa hh:mm form a Brazilian "
        "reads, in the timezone the footer promises"
    )
    assert f"{row.byte_size} bytes" in body, (
        "the row states no size, so a client cannot tell a real document from a "
        "truncated upload"
    )
    assert DOWNLOAD_LABEL in body, "no row offers a way to fetch the document back"
    assert f'href="{_download_path(row.storage_key)}"' in body, (
        "the row carries no download link built from this document's own storage key"
    )


# ==================================================== (c) the upload round trip


def test_an_upload_returns_to_the_vault_with_the_confirmation_rendered(
    firm: Firm,
) -> None:
    """POST, redirect, and the success notice drawn on the page it landed on.

    Three separate claims, and only the last of them is new. That the row is stored is
    already pinned in `tests/portal/test_document_vault.py`; what this adds is that the
    caller is returned to the list rather than to the landing page, and that the
    confirmation actually reaches a rendered document. The message queue is written
    into the session in a response phase that runs OUTSIDE the portal transaction, so
    "the words appear" is a statement about ordering as much as about copy.
    """
    http = firm.signed_in_as(firm.owner)
    filename = "recibo-novo.pdf"

    redirected = _upload(http, b"conteudo inteiramente novo", filename)
    assert redirected.status_code == HTTPStatus.FOUND, redirected.status_code
    assert redirected["Location"] == _list_path(), (
        f"the upload sent the client to {redirected['Location']!r} rather than back to "
        f"the vault at {_list_path()!r}"
    )

    landed = _upload(
        http,
        b"outro conteudo inteiramente novo",
        "segundo.pdf",
        follow=True,
    )
    assert landed.status_code == HTTPStatus.OK
    assert landed.redirect_chain, "the upload did not redirect at all"
    assert landed.redirect_chain[-1][0] == _list_path()

    body = landed.content.decode()
    assert UPLOAD_SUCCESS in body, (
        f"{UPLOAD_SUCCESS!r} never reached the page the upload landed on, so the "
        f"client is told nothing about whether the file arrived"
    )
    assert "segundo.pdf" in body, (
        "the file that was just uploaded is absent from the list it redirected to, so "
        "the round trip stops short of showing the client their own evidence"
    )
    assert filename in body


def test_the_upload_form_is_a_plain_multipart_post_carrying_its_csrf_token(
    firm: Firm,
) -> None:
    """No JavaScript anywhere in the path a file takes off a client's phone.

    `{% csrf_token %}` is asserted to be the form's FIRST child rather than merely
    present: a hidden input placed after the file control still submits, so ordering is
    not a correctness property here — it is the habit that keeps the token from being
    dropped when the fields around it are rearranged.
    """
    body = _documents(firm.signed_in_as(firm.owner))

    form_at = body.find(f'action="{_upload_path()}"')
    assert form_at != -1, (
        f"no form posts to {_upload_path()}; the upload card either is not drawn or "
        f"names a route that is not the shipped upload endpoint"
    )
    form = body[body.rfind("<form", 0, form_at) : body.find("</form>", form_at)]

    assert 'method="post"' in form, "the upload form is not a POST"
    assert 'enctype="multipart/form-data"' in form, (
        "the upload form carries no multipart encoding, so the file never leaves the "
        "browser and the server sees an empty FILES"
    )
    assert 'type="file"' in form, "the form offers no native file control"
    assert 'name="file"' in form, (
        "the file control is not named `file`, which is the key the view reads out of "
        "request.FILES"
    )

    token_at = form.find("csrfmiddlewaretoken")
    assert token_at != -1, "the upload form carries no CSRF token"
    assert token_at < form.find('type="file"'), (
        "the CSRF token is not the form's first child"
    )
    assert TYPES_HINT in form, "the form names no accepted file types"
    assert SUBMIT_LABEL in form


def test_the_size_hint_names_the_cap_the_server_actually_enforces(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    """The hint moves when the setting moves, or it is decoration.

    A page promising a limit the server does not hold is worse than a page promising
    nothing: the client sizes their file to the number on the screen and is refused
    anyway, with no way to tell which of the two figures is real.
    """
    settings.PORTAL_UPLOAD_MAX_BYTES = SMALL_CAP
    narrow = _documents(firm.signed_in_as(firm.owner))
    assert SIZE_HINT.format(label=SMALL_CAP_LABEL) in narrow, (
        f"the page does not state a size limit of {SMALL_CAP_LABEL}, which is what "
        f"the server was configured to enforce for this render"
    )

    settings.PORTAL_UPLOAD_MAX_BYTES = LARGE_CAP
    wide = _documents(firm.signed_in_as(firm.owner))
    assert SIZE_HINT.format(label=LARGE_CAP_LABEL) in wide
    assert SIZE_HINT.format(label=SMALL_CAP_LABEL) not in wide, (
        "the size hint did not follow the setting, so it is a literal on the page "
        "rather than the cap the upload endpoint will apply"
    )


# ============================================ (d) the refusals, in pt-BR and at 400


def test_an_oversized_upload_renders_the_portuguese_error_page_at_400(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    """The status is pinned elsewhere; what is new is that a person can read it.

    `tests/portal/test_document_vault.py` already asserts BAD_REQUEST here and that
    nothing is stored. Neither of those says anything about what the client sees, and
    an unstyled one-line `HttpResponseBadRequest` is indistinguishable from the site
    being broken.

    The cap is moved rather than restated: `PORTAL_UPLOAD_MAX_BYTES` is the number the
    endpoint enforces, and a test spelling its own copy of it would keep passing after
    the setting changed.
    """
    settings.PORTAL_UPLOAD_MAX_BYTES = SMALL_CAP
    response = _upload(firm.signed_in_as(firm.owner), b"x" * (SMALL_CAP + 1))

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.status_code
    body = response.content.decode()
    assert ERROR_HEADING in body, (
        f"the refusal renders no page a client can read; {ERROR_HEADING!r} is absent"
    )
    assert TOO_LARGE_COPY.format(label=SMALL_CAP_LABEL) in body, (
        "the refusal does not say what the limit is, so the client cannot tell how "
        "much smaller the file needs to be"
    )
    assert HEADING in body, (
        "the error page is not drawn inside the portal shell, so a client who lands "
        "on it has no navigation back to anything"
    )


def test_an_upload_with_no_file_renders_the_portuguese_error_page_at_400(
    firm: Firm,
) -> None:
    """The same page, the other branch, and the same pinned status."""
    response = firm.signed_in_as(firm.owner).post(
        _upload_path(),
        {},
        headers={"host": PORTAL_HOST},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.status_code
    body = response.content.decode()
    assert ERROR_HEADING in body
    assert NO_FILE_COPY in body, (
        "the refusal does not tell the client what went wrong or what to do next"
    )


def test_the_error_page_offers_a_retry_that_carries_its_own_csrf_token(
    firm: Firm,
) -> None:
    """A dead end after a failed upload is a client who gives up.

    The retry is a real form rather than a link, so it needs its own token: a form
    re-rendered without one answers 403 on submit, which turns a recoverable mistake
    into a second, more confusing failure.
    """
    response = firm.signed_in_as(firm.owner).post(
        _upload_path(),
        {},
        headers={"host": PORTAL_HOST},
    )
    body = response.content.decode()

    form_at = body.find(f'action="{_upload_path()}"')
    assert form_at != -1, "the error page offers no way to try the upload again"
    form = body[body.rfind("<form", 0, form_at) : body.find("</form>", form_at)]
    assert "csrfmiddlewaretoken" in form, (
        "the retry form carries no CSRF token, so submitting it answers 403"
    )
    assert 'enctype="multipart/form-data"' in form


def test_the_error_page_is_covered_by_the_portal_template_guards() -> None:
    """It must live where the guard glob already looks, or nothing checks it.

    `tests/portal/test_portal_templates_render.py` walks
    `apps/portal/templates/portal/**/*.html`. A refusal page parked anywhere else is a
    portal template that no capability ban, no relation ban and no layout rule applies
    to — which is exactly the file such a rule would be most useful on.
    """
    assert UPLOAD_ERROR_TEMPLATE.exists(), (
        f"{UPLOAD_ERROR_TEMPLATE} does not exist; the refusal branches render no "
        f"template at all"
    )
    assert UPLOAD_ERROR_TEMPLATE.is_relative_to(PORTAL_TEMPLATES)


# ================================================ (e) another client's key: W6 again


def test_a_replayed_sibling_key_is_refused_without_asking_storage(firm: Firm) -> None:
    """W6, restated from the page's point of view rather than the endpoint's.

    A client who obtains a sibling's `storage_key` — from anywhere at all — and pastes
    it into the download route gets a 404 decided by the ROW lookup, before storage is
    invoked. Mirrored rather than imported from `tests/portal/test_document_vault.py`,
    as every guard in this package is; that module's own proof must stay green
    alongside this one.
    """
    http = firm.signed_in_as(firm.owner)

    with mock.patch.object(
        default_storage,
        "open",
        side_effect=AssertionError("storage was asked for bytes"),
    ) as opened:
        response = http.get(
            _download_path(firm.beta_key),
            headers={"host": PORTAL_HOST},
        )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.status_code
    assert opened.call_count == 0, (
        "storage was asked for another client's object before the refusal; under "
        "object storage that is a network call that has already left the building"
    )


def test_the_positive_control_reaches_storage_for_this_clients_own_key(
    firm: Firm,
) -> None:
    """Without this, the refusal above also passes against a view that never works."""
    http = firm.signed_in_as(firm.owner)

    with mock.patch.object(
        default_storage,
        "open",
        wraps=default_storage.open,
    ) as opened:
        response = http.get(
            _download_path(firm.alpha_key),
            headers={"host": PORTAL_HOST},
        )

    assert response.status_code == HTTPStatus.OK, response.status_code
    assert opened.call_count == 1


# ======================================================== (f) the empty state


def test_a_client_with_no_documents_is_told_so_rather_than_shown_a_void(
    bare_firm: Firm,
) -> None:
    """An empty vault and a broken page look identical without words for the difference.

    The sibling still holds documents, so this also asserts the empty state is ALPHA's
    emptiness rather than the table's.
    """
    body = _documents(bare_firm.signed_in_as(bare_firm.owner))

    assert EMPTY_STATE in body
    assert UPLOAD_HEADING in body, (
        "the empty state replaced the upload card, so a client with nothing in their "
        "vault has no way to put the first thing into it"
    )
    for filename in BETA_FILENAMES:
        assert filename not in body, (
            f"{filename} is the sibling's document on a page that just claimed to be "
            f"empty"
        )


# ================================================= the portal ships no JavaScript


@pytest.mark.parametrize(
    "template",
    [DOCUMENTS_TEMPLATE, UPLOAD_ERROR_TEMPLATE, UPLOAD_FORM_TEMPLATE],
    ids=lambda path: str(path.name),
)
def test_no_vault_template_ships_executable_or_inline_content(template: Path) -> None:
    """No script, no htmx, no inline handler, no inline style, no data URI.

    The portal is the one boundary in this product that row-level security is
    enforcing, and executable content shipped across it is a shared attack surface. A
    native file input and a plain multipart POST need none of it — there is deliberately
    no progress bar, because the browser already draws one.
    """
    assert template.exists(), f"{template} does not exist"
    source = template.read_text(encoding="utf-8")

    offenders = [
        f"{template.name}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if SCRIPTED.search(line)
    ]
    assert offenders == [], (
        f"{template.name} ships executable or inline content: {offenders}"
    )
