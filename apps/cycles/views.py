"""Playbook management views: the firm-side editor under `/gestao/playbooks/`.

Every route is flag-gated (`cycles_r1`) behind `require_tenant`, so a firm
without the entitlement gets the same 404 as a firm that does not exist, and
capability-gated on `PLAYBOOK_GATE` — the S8.1 `playbooks.manage` cell, read
through `apps.cycles.capabilities` until the T4 matrix lands. The screens are a
typed form list (S5.4 M3): list, detail, and one editor per draft version, with
moves and publishes as named POST actions — no drag-only interaction anywhere.

Status map: a validation refusal answers 422, a lost optimistic race 409, a
missing or foreign row 404, and `require_can` refuses 403 before any of them.
"""

from collections.abc import Sequence
from http import HTTPStatus
from typing import cast
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.forms.formsets import DELETION_FIELD_NAME, BaseFormSet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.core.flags import require_feature
from apps.cycles.capabilities import PLAYBOOK_GATE
from apps.cycles.errors import (
    IllegalTransition,
    ImmutableVersion,
    PlaybookValidationError,
    VersionConflict,
)
from apps.cycles.forms import (
    NewPlaybookForm,
    PlaybookForm,
    StepForm,
    StepFormSet,
)
from apps.cycles.models import Playbook, PlaybookVersion, VersionState
from apps.cycles.packs import install_system_packs
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from apps.tenants.models import Tenant

FLAG = "cycles_r1"


def _tenant_of(request: AuthenticatedRequest) -> Tenant:
    """Return the tenant `require_tenant` already resolved for this request.

    `request.tenant` is set dynamically by `TenantMiddleware`, so the typed
    request does not declare it; `require_tenant` above the view is what makes
    the attribute's presence a guarantee rather than a hope.
    """
    return cast("Tenant", request.tenant)  # type: ignore[attr-defined]  # set by TenantMiddleware


def _playbook_or_404(pk: UUID) -> Playbook:
    """Resolve the playbook inside this tenant, or answer nothing exists.

    `objects` — the tenant-scoped manager — plus RLS means a foreign pk is a
    plain miss: a 403 would name the row's existence, a 404 does not.
    """
    playbook = Playbook.objects.filter(pk=pk).first()
    if playbook is None:
        raise Http404
    return playbook


def _version_or_404(playbook: Playbook, number: int) -> PlaybookVersion:
    """Resolve the numbered version on the playbook, or 404."""
    version = playbook.versions.filter(number=number).first()
    if version is None:
        raise Http404
    return version


def _steps_as_initial(version: PlaybookVersion) -> list[dict[str, object]]:
    return [StepForm.initial_of(step) for step in version.steps.order_by("position")]


def _editor_context(  # noqa: PLR0913, PLR0917 — the context names them all
    playbook: Playbook,
    version: PlaybookVersion,
    form: PlaybookForm,
    formset: BaseFormSet[StepForm],
    preview: playbooks.PublishPreview | None = None,
    errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "playbook": playbook,
        "version": version,
        "form": form,
        "formset": formset,
        "preview": preview,
        "errors": errors or [],
        "is_draft": version.state == VersionState.DRAFT,
    }


def _editor_errors(
    request: AuthenticatedRequest,
    context: dict[str, object],
    errors: Sequence[object],
    *,
    status: int,
) -> HttpResponse:
    """Re-render the editor with a banner naming every problem found.

    A 422/409 re-render rather than a redirect keeps the submitted field values
    on screen — losing the draft to a validation error is how editors lose work.
    """
    return render(
        request,
        "cycles/playbook_editor.html",
        {**context, "errors": [str(error) for error in errors]},
        status=status,
    )


def _formset_of(
    request: HttpRequest,
    version: PlaybookVersion,
) -> BaseFormSet[StepForm]:
    if request.method == "POST":
        return StepFormSet(request.POST, prefix="steps")
    return StepFormSet(prefix="steps", initial=_steps_as_initial(version))


def _specs_of_valid_formset(formset: BaseFormSet[StepForm]) -> list[StepSpec]:
    """Parse the bound rows into StepSpecs; callers check `is_valid()` first.

    `cleaned_data` decides inclusion: a row whose fields failed to parse never
    reaches it, and the `is_valid()` gate above keeps us from reading it. The
    trailing extra row validates as `empty_permitted` and carries no `key`, so
    it is skipped rather than becoming an empty step; `can_delete` rows are
    dropped the same way.
    """
    specs: list[StepSpec] = []
    for form in formset:
        if formset.can_delete and form.cleaned_data.get(DELETION_FIELD_NAME):
            continue
        if not form.cleaned_data.get("key"):
            continue
        specs.append(form.to_spec())
    return specs


def _expectation(request: HttpRequest) -> int | None:
    """Read the optimistic counter the page submitted — required, not defaulted.

    The counter is rendered into the form; a POST without one was not made from
    this page and must not write at the current revision. `None` means missing
    or malformed; callers translate it to 409 with no writes.
    """
    raw = request.POST.get("expected_version")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


# Names the signature's purpose inside the key; not a credential, a scope pin.
PREVIEW_TOKEN_SALT = "cycles.preview-publish.v1"  # noqa: S105
_SIGNER = TimestampSigner(salt=PREVIEW_TOKEN_SALT)


def _preview_token(version: PlaybookVersion) -> str:
    """Sign the revision a preview panel was rendered for."""
    return _SIGNER.sign(f"{version.pk}:{version.version}")


def _preview_token_ok(version: PlaybookVersion, token: str) -> bool:
    """Report whether the token names this version's CURRENT stored revision."""
    try:
        unsigned = _SIGNER.unsign(token)
    except (BadSignature, SignatureExpired):
        return False
    return unsigned == f"{version.pk}:{version.version}"


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(PLAYBOOK_GATE)
def playbook_list_view(request: AuthenticatedRequest) -> HttpResponse:
    """List the firm's playbooks; POST creates, clones or opens a draft.

    `install_system_packs` runs on entry: the flag may have been granted before
    this deploy or while the signal's module was absent, and the installer is
    idempotent by contract, so the list is always complete after the first view.
    """
    tenant = _tenant_of(request)
    if request.method == "POST":
        response = _list_post(request, tenant)
        if response is not None:
            return response
        return redirect("playbook-list")
    install_system_packs(tenant)
    rows = Playbook.objects.order_by("key").prefetch_related("versions")
    return render(
        request,
        "cycles/playbook_list.html",
        {
            "rows": rows,
            "create_form": NewPlaybookForm(),
            "columns": [
                {"label": _("Nome")},
                {"label": _("Chave")},
                {"label": _("Origem")},
                {"label": _("Versões")},
                {"label": _("Ações")},
            ],
        },
    )


def _list_post(request: AuthenticatedRequest, tenant: Tenant) -> HttpResponse | None:
    """Dispatch the list's POST actions, or return None for a bare redirect."""
    action = request.POST.get("action")
    try:
        if action == "clone":
            playbooks.clone_playbook(
                actor=request.user,
                tenant=tenant,
                playbook_pk=UUID(str(request.POST.get("playbook", ""))),
            )
        elif action == "create":
            form = NewPlaybookForm(request.POST)
            if not form.is_valid():
                messages.error(request, _("Confira os campos do novo playbook."))
                return None
            playbook = playbooks.create_playbook(
                actor=request.user,
                tenant=tenant,
                key=str(form.cleaned_data["key"]),
                name=str(form.cleaned_data["name"]),
                applies_to=form.applies_to(),
            )
            return redirect("playbook-detail", pk=playbook.pk)
        elif action == "new_draft":
            draft = playbooks.create_draft_version(
                actor=request.user,
                tenant=tenant,
                playbook_pk=UUID(str(request.POST.get("playbook", ""))),
            )
            return redirect(
                "playbook-editor", pk=draft.playbook_id, number=draft.number
            )
        else:
            raise Http404
    except (
        PlaybookValidationError,
        VersionConflict,
        IllegalTransition,
        ImmutableVersion,
    ) as exc:
        messages.error(request, str(exc))
    # A playbook pk that is absent or belongs to another tenant is a 404, the
    # same refusal the GETs give — never a 500 and never a leak-shaped denial.
    except (Playbook.DoesNotExist, PlaybookVersion.DoesNotExist) as exc:
        raise Http404 from exc
    except ValueError as exc:
        raise Http404 from exc
    return None


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(PLAYBOOK_GATE)
def playbook_detail_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Show one playbook: its versions and the entry into the editor."""
    playbook = _playbook_or_404(pk)
    versions = playbook.versions.order_by("-number")
    draft = next((v for v in versions if v.state == VersionState.DRAFT), None)
    return render(
        request,
        "cycles/playbook_detail.html",
        {
            "playbook": playbook,
            "versions": versions,
            "draft": draft,
            "columns": [
                {"label": _("Versão")},
                {"label": _("Estado")},
                {"label": _("Publicada em")},
                {"label": _("Ações")},
            ],
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(PLAYBOOK_GATE)
def playbook_editor_view(
    request: AuthenticatedRequest,
    pk: UUID,
    number: int,
) -> HttpResponse:
    """Render the typed step-list editor for one draft version.

    POST actions: `save` rewrites the draft, `move` reorders one step a
    position, `preview` saves then reports what publishing would generate, and
    `publish` validates and flips the state — the same validation the preview
    ran, re-run under the lock so a stale preview cannot publish a broken DAG.
    """
    playbook = _playbook_or_404(pk)
    version = _version_or_404(playbook, number)
    if request.method == "GET":
        form = PlaybookForm(
            initial={
                "name": playbook.name,
                "applies_regimes": (version.applies_to_json or {}).get("regimes", []),
                "applies_service_kinds": (version.applies_to_json or {}).get(
                    "service_kinds", []
                ),
            }
        )
        return render(
            request,
            "cycles/playbook_editor.html",
            _editor_context(playbook, version, form, _formset_of(request, version)),
        )
    return _editor_post(request, playbook, version)


def _editor_post(  # noqa: PLR0911, C901 — each return IS one action's answer
    request: AuthenticatedRequest,
    playbook: Playbook,
    version: PlaybookVersion,
) -> HttpResponse:
    """Dispatch the editor's POST actions, mapping refusals onto statuses."""
    tenant = _tenant_of(request)
    form = PlaybookForm(request.POST)
    formset = _formset_of(request, version)
    context = _editor_context(playbook, version, form, formset)
    expected = _expectation(request)
    action = str(request.POST.get("action") or "")

    if expected is None:
        return _editor_errors(
            request,
            context,
            [
                _(
                    "A revisão do rascunho é obrigatória; recarregue a página "
                    "e tente de novo."
                )
            ],
            status=HTTPStatus.CONFLICT,
        )
    if version.state != VersionState.DRAFT:
        return _editor_errors(
            request,
            context,
            [_("Somente versões em rascunho podem ser editadas.")],
            status=HTTPStatus.CONFLICT,
        )
    try:
        if action == "move":
            moved = playbooks.move_step(
                actor=request.user,
                tenant=tenant,
                playbook_pk=playbook.pk,
                number=version.number,
                expected_version=expected,
                step_key=str(request.POST.get("step") or ""),
                direction=str(request.POST.get("direction") or ""),
            )
            return redirect("playbook-editor", pk=playbook.pk, number=moved.number)
        if action == "publish":
            return _publish_post(request, context, playbook, version, expected=expected)
        if action not in {"save", "preview"}:
            raise Http404
        if not (form.is_valid() and formset.is_valid()):
            return _editor_errors(
                request,
                context,
                [_("Confira os campos destacados antes de continuar.")],
                status=HTTPStatus.UNPROCESSABLE_CONTENT,
            )
        saved = playbooks.update_draft(
            actor=request.user,
            tenant=tenant,
            playbook_pk=playbook.pk,
            number=version.number,
            expected_version=expected,
            steps=_specs_of_valid_formset(formset),
            applies_to=form.applies_to(),
            name=str(form.cleaned_data["name"]),
        )
        return _editor_action(request, playbook, saved, action=action, form=form)
    except PlaybookValidationError as exc:
        return _editor_errors(
            request, context, exc.errors, status=HTTPStatus.UNPROCESSABLE_CONTENT
        )
    except VersionConflict:
        return _editor_errors(
            request,
            context,
            [
                _(
                    "Alguém alterou esta versão enquanto você editava. "
                    "Recarregue a página e tente de novo."
                )
            ],
            status=HTTPStatus.CONFLICT,
        )
    except (IllegalTransition, ImmutableVersion) as exc:
        return _editor_errors(request, context, [str(exc)], status=HTTPStatus.CONFLICT)
    except (Playbook.DoesNotExist, PlaybookVersion.DoesNotExist) as exc:
        raise Http404 from exc


def _editor_action(
    request: AuthenticatedRequest,
    playbook: Playbook,
    saved: PlaybookVersion,
    *,
    action: str,
    form: PlaybookForm,
) -> HttpResponse:
    """Finish `save`/`preview`/`publish` after the draft write succeeded."""
    tenant = _tenant_of(request)
    if action == "save":
        messages.success(request, _("Rascunho salvo."))
        return redirect("playbook-editor", pk=playbook.pk, number=saved.number)
    preview = playbooks.preview_publish(
        actor=request.user,
        tenant=tenant,
        playbook_pk=playbook.pk,
        number=saved.number,
    )
    fresh = _editor_context(
        playbook,
        saved,
        form,
        _formset_of(request, saved),
        preview=preview,
    )
    # The token binds the preview panel to the revision it described; publish
    # accepts only that token, so a draft edited after preview cannot be
    # published under the old count.
    fresh["preview_token"] = _preview_token(saved)
    return render(request, "cycles/playbook_editor.html", fresh)


def _publish_post(
    request: AuthenticatedRequest,
    context: dict[str, object],
    playbook: Playbook,
    version: PlaybookVersion,
    *,
    expected: int,
) -> HttpResponse:
    """Publish the revision the preview described — and only that revision.

    Publishing does not take field content: the token signs `pk:version`, so a
    draft changed since the preview refuses with 409 and must be previewed
    again. The service re-validates the locked draft either way.
    """
    token = str(request.POST.get("preview_token") or "")
    if not _preview_token_ok(version, token):
        return _editor_errors(
            request,
            context,
            [
                _(
                    "Publique a pré-visualização atual: gere a "
                    "pré-visualização desta versão e confirme a partir dela."
                )
            ],
            status=HTTPStatus.CONFLICT,
        )
    published, preview = playbooks.publish_version(
        actor=request.user,
        tenant=_tenant_of(request),
        playbook_pk=playbook.pk,
        number=version.number,
        expected_version=expected,
    )
    messages.success(
        request,
        _(
            "Versão %(number)s publicada: %(clients)s clientes no próximo "
            "período, %(pinned)s ciclos abertos fixados em versões anteriores."
        )
        % {
            "number": published.number,
            "clients": preview.matching_clients,
            "pinned": preview.pinned_open_cycles,
        },
    )
    return redirect("playbook-detail", pk=playbook.pk)


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(PLAYBOOK_GATE)
def playbook_retire_view(
    request: AuthenticatedRequest,
    pk: UUID,
    number: int,
) -> HttpResponse:
    """Retire a published version; cycles already pinned to it stay readable."""
    playbook = _playbook_or_404(pk)
    tenant = _tenant_of(request)
    _version_or_404(playbook, number)
    expected = _expectation(request)
    if expected is None:
        messages.error(
            request,
            _(
                "A revisão da versão é obrigatória; recarregue a página e "
                "tente de novo."
            ),
        )
        return redirect("playbook-detail", pk=playbook.pk)
    try:
        playbooks.retire_version(
            actor=request.user,
            tenant=tenant,
            playbook_pk=playbook.pk,
            number=number,
            expected_version=expected,
        )
    except (VersionConflict, IllegalTransition) as exc:
        messages.error(request, str(exc))
    except PlaybookVersion.DoesNotExist as exc:
        raise Http404 from exc
    return redirect("playbook-detail", pk=playbook.pk)


__all__ = [
    "playbook_detail_view",
    "playbook_editor_view",
    "playbook_list_view",
    "playbook_retire_view",
]
