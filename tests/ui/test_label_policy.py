import ast
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from django.utils.translation import gettext, override

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
APPS_ROOT: Final[Path] = PROJECT_ROOT / "apps"

PT_BR_LABELS: Final[frozenset[str]] = frozenset(
    {
        "CNPJ",
        "CPF",
        "Confirmação da senha",
        "E-mail",
        "Empresa",
        "Motivo (obrigatório para acesso fora das suas empresas)",
        "Nome completo",
        "Papel",
        "Relacionamento",
        "Senha",
        "Tipo de solicitação",
    }
)
APPROVED_ENGLISH_LABELS: Final[dict[str, str]] = {}


@dataclass(frozen=True)
class LabelUse:
    location: str
    value: str | None


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                constants[node.target.id] = node.value
    return constants


def _label_value(
    node: ast.expr,
    constants: Mapping[str, ast.expr],
    resolving: frozenset[str] = frozenset(),
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:
        return _label_value(node.args[0], constants, resolving)
    if isinstance(node, ast.Name) and node.id in constants and node.id not in resolving:
        return _label_value(
            constants[node.id],
            constants,
            resolving | {node.id},
        )
    return None


def _form_classes(tree: ast.Module) -> list[ast.ClassDef]:
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    form_names = {
        name
        for name, node in classes.items()
        if any(_base_name(base) in {"Form", "ModelForm"} for base in node.bases)
    }
    changed = True
    while changed:
        inherited = {
            name
            for name, node in classes.items()
            if any(_base_name(base) in form_names for base in node.bases)
        }
        changed = not inherited.issubset(form_names)
        form_names.update(inherited)
    return [classes[name] for name in sorted(form_names)]


def _form_label_uses(source: str, name: str) -> tuple[int, list[LabelUse]]:
    tree = ast.parse(source, filename=name)
    constants = _module_constants(tree)
    forms = _form_classes(tree)
    uses = [
        LabelUse(
            f"{name}:{keyword.value.lineno}",
            _label_value(keyword.value, constants),
        )
        for form in forms
        for node in ast.walk(form)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "label"
    ]
    return len(forms), uses


def _bundled_translation(msgid: str) -> str:
    with override("pt-br"):
        return gettext(msgid)


def _label_offenders(
    uses: Collection[LabelUse],
    pt_br_labels: Collection[str],
    approved_english: Mapping[str, str],
) -> list[str]:
    offenders: list[str] = []
    for use in uses:
        if use.value in pt_br_labels:
            continue
        if use.value is None:
            offenders.append(
                f"{use.location}: label is not a resolvable string literal"
            )
            continue
        if use.value not in approved_english:
            offenders.append(f"{use.location}: unapproved label {use.value!r}")
            continue
        reason = approved_english[use.value]
        translated = _bundled_translation(use.value)
        if not reason.strip():
            offenders.append(
                f"{use.location}: approved-English label {use.value!r} has no reason"
            )
        elif translated == use.value:
            offenders.append(
                f"{use.location}: approved-English label {use.value!r} is not "
                "translated by Django's bundled pt-BR catalogue"
            )
    return sorted(offenders)


def _application_label_offenders(
    approved_english: Mapping[str, str] = APPROVED_ENGLISH_LABELS,
) -> list[str]:
    sources = sorted(
        path
        for path in APPS_ROOT.rglob("*.py")
        if "migrations" not in path.parts and "__pycache__" not in path.parts
    )
    assert len(sources) > 100, "the form-label policy found no application tree"
    form_count = 0
    uses: list[LabelUse] = []
    for path in sources:
        relative = str(path.relative_to(PROJECT_ROOT))
        count, found = _form_label_uses(path.read_text(encoding="utf-8"), relative)
        form_count += count
        uses.extend(found)
    assert form_count > 5, "the form-label policy found no project-owned forms"
    assert len(uses) > 10, "the form-label policy found no label= declarations"
    assert all(reason.strip() for reason in approved_english.values()), (
        "every approved-English label must carry a written reason"
    )
    return _label_offenders(uses, PT_BR_LABELS, approved_english)


def test_project_form_labels_follow_the_language_policy() -> None:
    offenders = _application_label_offenders()

    assert offenders == [], "form label policy violation:\n  " + "\n  ".join(offenders)


def test_the_label_policy_reports_english_and_accepts_both_approved_paths() -> None:
    planted = """
class PlantedForm(forms.Form):
    bad = forms.CharField(label=_("relationship"))
    local = forms.CharField(label=_("Relacionamento"))
    bundled = forms.CharField(label=_("password"))
"""
    _, uses = _form_label_uses(planted, "apps/plantado.py")
    approved = {
        "password": (
            "Django's bundled pt-BR catalogue renders this msgid as 'senha'; the "
            "synthetic entry proves catalogue-backed English remains supported."
        )
    }

    offenders = _label_offenders(uses, PT_BR_LABELS, approved)

    assert offenders == ["apps/plantado.py:3: unapproved label 'relationship'"]
    dishonest = _label_offenders(
        uses[:1],
        PT_BR_LABELS,
        {"relationship": "Planted dishonest exemption."},
    )
    assert dishonest == [
        (
            "apps/plantado.py:3: approved-English label 'relationship' is not "
            "translated by Django's bundled pt-BR catalogue"
        )
    ]


def test_the_shipped_approved_english_allow_set_is_explicit_and_empty() -> None:
    assert APPROVED_ENGLISH_LABELS == {}
