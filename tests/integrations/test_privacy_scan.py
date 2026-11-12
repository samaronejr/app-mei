"""Known-value scan equivalence and repetitive-input latency regressions."""

from hashlib import shake_256
from time import perf_counter

import pytest

from apps.integrations.privacy import (
    MARKER,
    RedactionBudgetError,
    redact_structure,
    scrub_text,
)
from tests.integrations.privacy_scan_reference import redact_structure as baseline

DIAGNOSTICS = "status=503 operation=SP-PAG elapsed_ms=37 request_id=RV7-2701"
RANGE_CASES = (
    ("rv7ALPHABRAVOCHARLIE", ("rv7ALPHABRAVOCHARLIE", "BRAVO")),
    ("rv7ALPHArv7BRAVO", ("rv7ALPHA", "rv7BRAVO")),
    ("rv7ALPHABRAVO", ("rv7ALPHA", "rv7ALPHABRAVO")),
    ("rv7ALPHABRAVOCHARLIE", ("rv7ALPHABRAVO", "BRAVOCHARLIE")),
    ("rv7ALPHABRAVOCHARLIE", ("BRAVOCHARLIE", "rv7ALPHABRAVO")),
    (
        "rv7A--rv7B--rv7C--rv7D--rv7E",
        ("rv7A--rv7B", "rv7B--rv7C", "rv7C--rv7D", "rv7D--rv7E"),
    ),
    ("uvwxuvwxuvwxuvwx", ("uvwxuvwxuv",)),
    ("rv7ALPHABRAVOCHARLIE", ("rv7ALPHA", "rv7ALPHABRAVO", "BRAVOCHARLIE")),
    ("rv7ALPHA;rv7BRAVO rv7ALPHA", ("rv7ALPHA", "rv7BRAVO")),
    # Failure-link outputs, fallback transitions, and a late enclosing match.
    ("ushers; his; she", ("he", "she", "his", "hers")),
    ("shez", ("he", "shex")),
    ("abacababax; abax", ("abac", "abab", "bax")),
    ("ab--cd--ef", ("ab", "cd", "ef", "ab--cd--ef")),
    ("ab--cd--ef gh", ("ab", "cd", "ef", "--cd--ef", "gh")),
    ("[Filtered]tail; Filtered", ("Filtered", "[Filtered]tail")),
    ("aa aaa aaaa", ("", "a", "aa", "aa")),
    (
        "\u00e7\u00f8\u00e7\u00f8\u00e7\u00f8; \udcffxy",
        ("\u00e7\u00f8\u00e7", "\udcffx"),
    ),
)


@pytest.mark.parametrize(("core", "known"), RANGE_CASES)
def test_scan_matches_approved_overlapping_attack_output(
    core: str, known: tuple[str, ...]
) -> None:
    # Given the rv7 range corpus plus failure-link and marker boundaries.
    message = f"keep {core} {DIAGNOSTICS}"
    # When the public redactor handles text and bytes, before and after rescrubbing.
    for value in (message, message.encode("utf-8", "surrogateescape")):
        expected = baseline(value, set(known))
        actual = redact_structure(value, set(known))
        # Then output is byte-for-byte identical to the approved algorithm.
        assert actual == expected
        assert redact_structure(actual, set(known)) == baseline(expected, set(known))


def test_scan_matches_approved_seeded_overlapping_inputs() -> None:
    # Given 512 overlapping cases from a fixed seeded byte stream, not process hash.
    for alphabet in ("uvwxzyUVWXYZ", "aab"):
        for case in range(256):
            sample = shake_256(f"2701:{alphabet}:{case}".encode()).digest(96)
            core = "".join(alphabet[byte % len(alphabet)] for byte in sample[:80])
            intervals = [
                (start % 65, 2 + length % 14)
                for start, length in zip(sample[80::2], sample[81::2], strict=True)
            ]
            known = {core[start : start + length] for start, length in intervals}
            context = {
                "echo": f"keep {core} {DIAGNOSTICS}",
                "headers": {
                    f"X-Token-{index}": text for index, text in enumerate(sorted(known))
                },
            }
            # When credentials are discovered across the whole record.
            actual = redact_structure(context, set())
            # Then both credential discovery and coverage match the approved output.
            assert actual == baseline(context, set())


@pytest.mark.parametrize("field", ["body", "payload", "jwt_token"])
def test_scan_matches_approved_structural_crossing_output(field: str) -> None:
    # Given rv7's structural boundaries, including the disclosed closing-quote loss.
    for template, known in (
        ("FIELD='rv7IDENTICAL'", "rv7IDENTICAL"),
        ("prefix FIELD='rv7INNER' rv7OUTER", "FIELD='rv7INNER' rv7OUTER"),
        ("prefix FIELD='rv7HEAD;rv7TAIL'", "prefix FIELD='rv7HEAD"),
        ("FIELD='rv7CLOSINGabc' rv7PRIVATE", "c' rv7PRIVATE"),
    ):
        message = template.replace("FIELD", field) + " " + DIAGNOSTICS
        credentials = {known.replace("FIELD", field)}
        # When structural and known-value coverage cross.
        actual = scrub_text(message, credentials.copy())
        expected = baseline(message, credentials.copy())
        # Then no approved privacy or fail-closed rescrubbing behavior changes.
        assert actual == expected
        assert redact_structure(actual, set()) == baseline(expected, set())


@pytest.mark.parametrize(
    "known",
    [{"q" * 127}, {"q" * length for length in range(2, 129)}],
    ids=["long-discovered-value", "many-self-overlapping-values"],
)
def test_repetitive_known_value_scan_completes_under_one_second(
    known: set[str],
) -> None:
    # Given 256 KiB of repeats; discovery also remembers the whole base64-like run.
    message = "q" * (256 * 1024) + " " + DIAGNOSTICS
    # When the entire public scrub operation discovers and replaces overlapping data.
    started = perf_counter()
    actual = scrub_text(message, known.copy())
    elapsed = perf_counter() - started
    # Then all coverage collapses to one marker without quadratic scan work.
    assert actual == f"{MARKER} {DIAGNOSTICS}"
    assert elapsed < 1.0, f"256 KiB redaction took {elapsed:.3f}s"


@pytest.mark.parametrize("source", ["text", "known-values", "aggregate-text"])
def test_oversized_diagnostics_are_refused_before_unbounded_scan_allocation(
    source: str,
) -> None:
    # Given diagnostics beyond the scan's 512 Ki-character resource envelope.
    value: object = "ordinary diagnostic"
    known: set[str] = set()
    if source == "text":
        value = "? " * (300 * 1024)
    elif source == "known-values":
        known = {"q" * (600 * 1024)}
    else:
        value = ["? " * (150 * 1024), "! " * (150 * 1024)]
    # When discovery or index construction would exceed its resource budget.
    with pytest.raises(RedactionBudgetError):
        redact_structure(value, known)
    # Then callers can withhold the whole record rather than scan a private prefix.


def test_an_oversized_rendered_diagnostic_is_wholly_withheld_not_raised() -> None:
    # Given a public text caller that cannot discard a structured SDK event.
    text = "private diagnostic " * (40 * 1024)
    # When the diagnostic exceeds the same resource envelope.
    result = scrub_text(text, set())
    # Then the entire result is the machine-consumed redaction sentinel.
    assert result == MARKER
