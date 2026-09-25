"""Hold ops/PILOT-RUNBOOK.md to its declared shape.

The file's first line lists the 26 required section slugs. Each must appear exactly
once as a level-two heading anchor, and no level-two anchor may exist outside that
list, so a section can be neither dropped nor quietly added. Only two angle-bracket
tokens may remain unfilled, and the tracked file must carry no email address or phone
number: contacts live in the password manager, never here.
"""

import re
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
RUNBOOK: Final = PROJECT_ROOT / "ops" / "PILOT-RUNBOOK.md"

REQUIRED_COUNT: Final = 26
ALLOWED_TOKENS: Final = frozenset({"<pilot-slug>", "<production-host>"})

REQUIRED_COMMENT: Final = re.compile(r"<!-- required-sections: ([a-z -]+?) -->")
SECTION_ANCHOR: Final = re.compile(r"^## .+ \{#([a-z-]+)\}$", re.MULTILINE)
TOKEN: Final = re.compile(r"<[a-z-]*>")
EMAIL_OR_PHONE: Final = re.compile(r"@[a-z0-9.-]+\.[a-z]{2,}|\+55", re.IGNORECASE)


def _required(text: str) -> list[str]:
    match = REQUIRED_COMMENT.search(text)
    assert match is not None, "required-sections comment is missing"
    return match.group(1).split()


def _section_problems(text: str) -> list[str]:
    """Return one line per missing, duplicated, or undeclared section anchor."""
    required = _required(text)
    anchors = SECTION_ANCHOR.findall(text)
    problems = [f"MISSING {slug}" for slug in required if anchors.count(slug) == 0]
    problems += [f"DUPLICATE {slug}" for slug in required if anchors.count(slug) > 1]
    problems += [f"UNDECLARED {slug}" for slug in sorted(set(anchors) - set(required))]
    return problems


def test_required_list_declares_26_distinct_slugs() -> None:
    required = _required(RUNBOOK.read_text(encoding="utf-8"))

    assert len(required) == REQUIRED_COUNT
    assert len(set(required)) == REQUIRED_COUNT


def test_every_required_section_exists_once_and_no_other_exists() -> None:
    assert _section_problems(RUNBOOK.read_text(encoding="utf-8")) == []


def test_checker_names_a_deleted_section() -> None:
    # Positive control: the checker must be able to fail on the real file.
    text = RUNBOOK.read_text(encoding="utf-8")
    heading = "## Low disk {#low-disk}\n"
    assert text.count(heading) == 1

    assert _section_problems(text.replace(heading, "")) == ["MISSING low-disk"]


def test_checker_names_an_added_section() -> None:
    text = RUNBOOK.read_text(encoding="utf-8") + "\n## Extra {#extra-section}\n"

    assert _section_problems(text) == ["UNDECLARED extra-section"]


def test_only_the_two_deliberate_tokens_remain() -> None:
    tokens = set(TOKEN.findall(RUNBOOK.read_text(encoding="utf-8")))

    assert tokens == ALLOWED_TOKENS


def test_no_email_address_or_phone_number() -> None:
    hits = EMAIL_OR_PHONE.findall(RUNBOOK.read_text(encoding="utf-8"))

    assert hits == []
    # Positive control: the pattern does match the shapes it exists to keep out.
    assert EMAIL_OR_PHONE.search("owner at name@example.com.br") is not None
    assert EMAIL_OR_PHONE.search("call +55 00 00000-0000") is not None
