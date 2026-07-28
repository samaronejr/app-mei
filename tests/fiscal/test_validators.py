"""The CNPJ and CPF validators, including the two things that are counter-intuitive.

Read this before changing anything here.

**Módulo 11 over a 36-character alphabet has collisions, and that is correct.**
Character values are `ord(c) - 48`, so they span 0..42 — wider than the modulus. Two
characters whose values differ by a multiple of 11 are interchangeable in any of the
twelve base positions without altering either check digit. `12.LBC.345/01DE-35` is a
genuinely valid CNPJ derived from Receita Federal's own worked example by A→L, and
RFB's published reference implementation agrees. The obvious property test — "mutating
any single character invalidates the CNPJ" — is therefore mathematically false, and
Hypothesis finds the counterexample within a few hundred examples. The property
asserted here is the true one: mutating a character to one of a *different* residue
invalidates the checksum.

**The published arithmetic accepts degenerate inputs.** `00000000000000` has a
weighted sum of zero and check digits that are correctly `00`; every repeated-digit
CPF likewise satisfies its own checksum. Neither is a real document. They are rejected
by an explicit rule, and the tests below assert the rejection comes from that rule
rather than from the arithmetic — otherwise a later "simplification" that deleted the
rule would still look green.

The residue table is never written down here. It is generated from the alphabet and
its *properties* are asserted, because three separate review rounds of this project
each produced a wrong hand-written copy.
"""

from collections import defaultdict

import pytest
from django.core.exceptions import ValidationError
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from apps.fiscal.validators import (
    CNPJ_ALPHABET,
    CNPJ_BASE_LENGTH,
    CNPJ_LENGTH,
    CPF_LENGTH,
    cnpj_check_digit_remainder,
    cnpj_check_digits,
    cpf_check_digits,
    normalize_document,
    validate_cnpj,
    validate_cpf,
)

# Receita Federal publishes exactly ONE worked example. Everything else below that
# looks like a test vector is self-computed and labelled DERIVED.
OFFICIAL_CNPJ = "12ABC34501DE35"

# Derived by our own generator and independently confirmed against RFB's published
# reference implementation in T-031. See .evidence/T-031-happy.txt.
DERIVED_CNPJS = (
    "ABCDEFGHIJKL80",
    "ZZZZZZZZZZZZ62",
    "A1B2C3D4E5F668",
    "0A0A0A0A0A0A90",
    "MEI00000000120",
    "XY9Z8W7V6U5T72",
)

# Real, publicly listed companies. These prove the alphanumeric algorithm is a strict
# generalization: legacy numeric CNPJs validate under it unchanged.
LEGACY_NUMERIC_CNPJS = (
    "11222333000181",
    "00000000000191",
    "33000167000101",
    "47960950000121",
    "33592510000154",
)

VALID_CPFS = (
    "11144477735",
    "52998224725",
    "12345678909",
    "98765432100",
)


def _residue_classes() -> dict[int, list[str]]:
    """Generate the collision classes. Never transcribe this table — build it."""
    classes: defaultdict[int, list[str]] = defaultdict(list)
    for char in CNPJ_ALPHABET:
        classes[(ord(char) - 48) % 11].append(char)
    return dict(classes)


# --------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "12.ABC.345/01DE-35",
        "12ABC34501DE35",
        "12abc34501de35",
        "12.abc.345/01de-35",
        "  12.ABC.345/01DE-35  ",
    ],
)
def test_the_official_vector_is_accepted_in_every_spelling(raw: str) -> None:
    # Given the one CNPJ Receita Federal publishes a worked calculation for
    # When it arrives masked, unmasked, lowercase, or padded
    # Then it normalizes to the same stored value and validates
    assert normalize_document(raw) == OFFICIAL_CNPJ
    validate_cnpj(raw)


def test_lowercase_is_normalized_rather_than_rejected() -> None:
    # Given a user who typed a CNPJ in lowercase
    # When it is validated
    # Then it is accepted. Rejecting it would be a data-entry trap, and RFB's own
    # reference implementation uppercases its input before computing.
    validate_cnpj("12abc34501de35")
    assert normalize_document("12abc34501de35") == OFFICIAL_CNPJ


# --------------------------------------------------------------------------------
# Known-good vectors
# --------------------------------------------------------------------------------


def test_the_single_official_rfb_vector_validates() -> None:
    # Given RFB's published worked example
    # When validated
    # Then it passes, and our generator reproduces its check digits
    validate_cnpj(OFFICIAL_CNPJ)
    assert cnpj_check_digits(OFFICIAL_CNPJ[:CNPJ_BASE_LENGTH]) == "35"


@pytest.mark.parametrize("cnpj", DERIVED_CNPJS)
def test_derived_vectors_validate(cnpj: str) -> None:
    # Given a self-computed vector (DERIVED — not published by RFB)
    # When validated
    # Then it passes and round-trips through the generator
    validate_cnpj(cnpj)
    assert cnpj_check_digits(cnpj[:CNPJ_BASE_LENGTH]) == cnpj[CNPJ_BASE_LENGTH:]


def test_there_are_at_least_five_derived_vectors() -> None:
    # Given the acceptance criterion "one official vector plus >= 5 derived"
    # When the derived corpus is counted
    # Then it satisfies it, and this assertion fails if someone trims the corpus
    assert len(DERIVED_CNPJS) >= 5


@pytest.mark.parametrize("cnpj", LEGACY_NUMERIC_CNPJS)
def test_legacy_numeric_cnpjs_validate_under_the_same_algorithm(cnpj: str) -> None:
    # Given a real all-numeric CNPJ issued before the alphanumeric format
    # When validated by the alphanumeric validator
    # Then it passes. Both formats coexist; there is not a second code path.
    validate_cnpj(cnpj)


# --------------------------------------------------------------------------------
# THE DOCUMENTED COLLISION. Do not "fix" this.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("substitute", "cnpj"),
    [
        ("A->L", "12LBC34501DE35"),
        ("A->W", "12WBC34501DE35"),
        ("A->6", "126BC34501DE35"),
    ],
)
def test_a_same_residue_substitution_yields_another_valid_cnpj(
    substitute: str,
    cnpj: str,
) -> None:
    """`12.LBC.345/01DE-35` IS VALID. This is intended, not a bug.

    `'6'`=6, `'A'`=17, `'L'`=28 and `'W'`=39 are all ≡ 6 (mod 11), so substituting one
    for another in the official example changes the weighted sum by a multiple of 11
    and leaves both check digits at `35`. Receita Federal's own reference
    implementation reports all three as valid — verified in T-031.

    A future maintainer who "fixes" this by rejecting these numbers would be rejecting
    CNPJs that Receita Federal issues and accepts.
    """
    # Given the official vector with one character replaced by a same-residue one
    # When validated
    # Then it is valid, and its check digits are unchanged from the original
    assert cnpj != OFFICIAL_CNPJ, f"{substitute} did not change the number"
    validate_cnpj(cnpj)
    assert cnpj[CNPJ_BASE_LENGTH:] == OFFICIAL_CNPJ[CNPJ_BASE_LENGTH:]


# --------------------------------------------------------------------------------
# The residue table: GENERATED, and asserted by property
# --------------------------------------------------------------------------------


def test_every_residue_class_is_non_empty() -> None:
    # Given the generated collision classes
    classes = _residue_classes()

    # When each residue 0..10 is looked up
    # Then all eleven are populated
    assert sorted(classes) == list(range(11))
    assert all(classes[residue] for residue in range(11))


def test_the_residue_classes_partition_the_whole_alphabet() -> None:
    # Given the generated collision classes
    classes = _residue_classes()

    # When their members are pooled
    members = [char for group in classes.values() for char in group]

    # Then every one of the 36 permitted characters appears exactly once. A class map
    # that silently dropped characters would make the mutation property below vacuous.
    assert len(CNPJ_ALPHABET) == 36
    assert sorted(members) == sorted(CNPJ_ALPHABET)
    assert len(members) == len(set(members))


def test_the_collision_classes_span_digits_as_well_as_letters() -> None:
    """A substitution generator restricted to A-Z would miss most of the collisions.

    The exact structure, generated rather than assumed: each of residues 0..9 holds
    exactly one digit — a digit's value IS its residue — while residue 10 holds no
    digit at all, because no single character `0`-`9` has value 10. Every residue holds
    at least one letter, since the letters run 17..42 and cover a full cycle twice over.
    """
    # Given the generated collision classes
    classes = _residue_classes()

    # When digits and letters are counted within each
    digits_per_class = {
        residue: [char for char in group if char.isdigit()]
        for residue, group in classes.items()
    }

    # Then residues 0..9 hold exactly one digit each, and residue 10 holds none
    for residue in range(10):
        assert len(digits_per_class[residue]) == 1, residue
        assert digits_per_class[residue][0] == str(residue)
    assert digits_per_class[10] == []

    # And every residue holds at least one letter, so every class is a real
    # substitution class rather than a singleton
    for residue, group in classes.items():
        assert any(char.isalpha() for char in group), (residue, group)

    # And the classes a mutation generator must draw from therefore contain BOTH
    # digits and letters. Drawing substitutes from "A"-"Z" alone would never produce
    # the A->6 collision asserted above.
    mixed = [
        group
        for group in classes.values()
        if any(c.isdigit() for c in group) and any(c.isalpha() for c in group)
    ]
    assert len(mixed) == 10, "residues 0..9 each mix a digit with letters"


def test_same_class_substitution_preserves_both_check_digits_everywhere() -> None:
    """The collision is a property of the algorithm, not of one lucky example."""
    # Given the official base and every residue class
    base = OFFICIAL_CNPJ[:CNPJ_BASE_LENGTH]
    expected = cnpj_check_digits(base)
    classes = _residue_classes()

    # When every base position is substituted with every same-residue character
    checked = 0
    for position in range(CNPJ_BASE_LENGTH):
        group = classes[(ord(base[position]) - 48) % 11]
        for substitute in group:
            mutated = base[:position] + substitute + base[position + 1 :]

            # Then both check digits are unchanged, at every position, for every class
            assert cnpj_check_digits(mutated) == expected, (
                f"position {position}: {base[position]}->{substitute}"
            )
            checked += 1

    # And the sweep was not vacuous
    assert checked >= CNPJ_BASE_LENGTH * 2


# --------------------------------------------------------------------------------
# Property tests
# --------------------------------------------------------------------------------

_base_strategy = st.text(alphabet=CNPJ_ALPHABET, min_size=12, max_size=12)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(base=_base_strategy)
def test_every_generated_cnpj_validates(base: str) -> None:
    # Given any twelve permitted characters
    # When check digits are computed for them
    cnpj = base + cnpj_check_digits(base)

    # Then the result validates, unless it is one of the degenerate numbers that the
    # explicit rule refuses on purpose
    assume(len(set(cnpj)) > 1)
    validate_cnpj(cnpj)
    assert len(cnpj) == CNPJ_LENGTH


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    base=_base_strategy,
    position=st.integers(min_value=0, max_value=CNPJ_BASE_LENGTH - 1),
    substitute=st.sampled_from(CNPJ_ALPHABET),
)
def test_mutating_to_a_different_residue_invalidates_the_checksum(
    base: str,
    position: int,
    substitute: str,
) -> None:
    """The TRUE mutation property. It needs BOTH exclusions, not just the obvious one.

    Exclusion 1, the residue classes: `A`→`L` moves the weighted sum by a multiple of
    11 and changes nothing. This is the collision the plan documents.

    Exclusion 2, the clamp: `_clamp` maps remainders 0 AND 1 to the digit 0, so two
    operands on different residues can still share a check digit. Hypothesis found
    this one — `4U27B2I9S9YZ` → `4U27B2I949YZ`, remainders 1 and 0, both digit `00`
    — and it is asserted as its own regression case below. Filtering only on the
    residue class is NOT enough, and a test that did so would fail intermittently.
    """
    # Given a valid CNPJ and a substitute character of a different residue
    original = base[position]
    assume((ord(substitute) - 48) % 11 != (ord(original) - 48) % 11)
    cnpj = base + cnpj_check_digits(base)
    mutated = base[:position] + substitute + base[position + 1 :]

    # And given the mutation is not absorbed by the non-injective clamp
    remainders = {
        cnpj_check_digit_remainder(base),
        cnpj_check_digit_remainder(mutated),
    }
    assume(not remainders <= {0, 1})

    # When the mutated base is checked against the original's digits
    # Then the first check digit differs, so the tampered number is rejected
    assert cnpj_check_digits(mutated) != cnpj[CNPJ_BASE_LENGTH:]
    with pytest.raises(ValidationError) as caught:
        validate_cnpj(mutated + cnpj[CNPJ_BASE_LENGTH:])
    assert caught.value.code == "cnpj_check_digits"


def test_the_remainder_to_digit_clamp_is_not_injective() -> None:
    """Collision mechanism 2, distinct from the alphabet's residue classes.

    `11 - 1` is 10, which is not a single digit, so the algorithm clamps every
    remainder below 2 to the digit 0. Remainders 0 and 1 therefore become the same
    check digit, and a mutation that moves the sum between them is undetectable even
    though the characters involved are in different residue classes.
    """
    # Given the remainders a módulo-11 sum can produce
    digits = {r: (0 if r < 2 else 11 - r) for r in range(11)}

    # When they are mapped to check digits
    # Then exactly one pair collides, and it is {0, 1}
    collisions = [r for r in digits if list(digits.values()).count(digits[r]) > 1]
    assert sorted(collisions) == [0, 1]
    assert digits[0] == digits[1] == 0

    # And every other remainder maps to its own distinct digit
    others = {r: d for r, d in digits.items() if r not in {0, 1}}
    assert len(set(others.values())) == len(others)


def test_the_clamp_collision_is_reachable_with_real_cnpjs() -> None:
    """The concrete counterexample Hypothesis produced. Do not "fix" this either.

    `'S'` is 35 (≡ 2 mod 11) and `'4'` is 4 (≡ 4 mod 11) — genuinely different residue
    classes — yet both bases carry the check digits `00`, because their remainders are
    1 and 0. Receita Federal's own reference implementation reports both as valid.
    """
    # Given two bases differing at one position, in different residue classes
    original = "4U27B2I9S9YZ"
    mutated = "4U27B2I949YZ"
    assert original != mutated
    assert (ord("S") - 48) % 11 != (ord("4") - 48) % 11

    # When their remainders are computed
    # Then they differ, but both fall under the clamp
    assert cnpj_check_digit_remainder(original) == 1
    assert cnpj_check_digit_remainder(mutated) == 0

    # And both therefore produce the same check digits, and both validate
    assert cnpj_check_digits(original) == cnpj_check_digits(mutated) == "00"
    validate_cnpj(original + "00")
    validate_cnpj(mutated + "00")


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(base=st.text(alphabet="0123456789", min_size=9, max_size=9))
def test_every_generated_cpf_validates(base: str) -> None:
    # Given any nine digits
    # When check digits are computed
    cpf = base + cpf_check_digits(base)

    # Then the result validates unless it is a repeated-digit degenerate
    assume(len(set(cpf)) > 1)
    validate_cpf(cpf)
    assert len(cpf) == CPF_LENGTH


# --------------------------------------------------------------------------------
# Rejections, each with its own distinct message
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("12ABC34501DE3", "cnpj_length"),
        ("12ABC34501DE355", "cnpj_length"),
        ("", "cnpj_length"),
        ("12ABC34501D#35", "cnpj_characters"),
        ("12ABÇ34501DE35", "cnpj_characters"),
        ("12ABC34501DEA5", "cnpj_check_digit_characters"),
        ("12ABC34501DE34", "cnpj_check_digits"),
        ("12ABC34501DE36", "cnpj_check_digits"),
        ("00000000000000", "cnpj_degenerate"),
        ("11111111111111", "cnpj_degenerate"),
        ("AAAAAAAAAAAAAA", "cnpj_degenerate"),
    ],
)
def test_each_rejection_reports_its_own_reason(value: str, code: str) -> None:
    # Given an input that is wrong in exactly one way
    # When it is validated
    with pytest.raises(ValidationError) as caught:
        validate_cnpj(value)

    # Then the error names that way specifically, rather than a generic "invalid"
    assert caught.value.code == code


def test_the_length_and_check_digit_errors_are_distinguishable() -> None:
    """The T-032 failure criterion: a corrupted digit and a short string differ."""
    # Given a CNPJ with a deliberately corrupted final digit
    with pytest.raises(ValidationError) as corrupted:
        validate_cnpj("12ABC34501DE34")

    # And a thirteen-character string
    with pytest.raises(ValidationError) as short:
        validate_cnpj("12ABC34501DE3")

    # Then the two report different codes AND different human-readable messages
    assert corrupted.value.code != short.value.code
    assert str(corrupted.value) != str(short.value)
    assert "14" in str(short.value)


def test_a_check_digit_may_never_be_a_letter() -> None:
    # Given a CNPJ whose final two positions contain a letter
    # When it is validated
    with pytest.raises(ValidationError) as caught:
        validate_cnpj("12ABC34501DEA5")

    # Then it is refused for that reason specifically. The remainder rule can only
    # produce 0..9, and RFB's own format regex writes the check digits as \d{2}.
    assert caught.value.code == "cnpj_check_digit_characters"


# --------------------------------------------------------------------------------
# Degenerate inputs: rejected by RULE, not by arithmetic
# --------------------------------------------------------------------------------


def test_the_all_zero_cnpj_passes_the_arithmetic_and_is_still_refused() -> None:
    """Proves the degenerate rule is doing the work, not the checksum.

    If a later refactor deleted the explicit rule, the checksum alone would happily
    accept `00000000000000` — Receita Federal's own reference implementation does.
    Asserting only "it is rejected" would not catch that; asserting that the
    arithmetic accepts it *and* the validator refuses it does.
    """
    # Given the all-zero CNPJ
    # When its check digits are computed from its own base
    # Then the arithmetic agrees with it
    assert cnpj_check_digits("0" * CNPJ_BASE_LENGTH) == "00"

    # And yet the validator refuses it, by the degenerate rule
    with pytest.raises(ValidationError) as caught:
        validate_cnpj("0" * CNPJ_LENGTH)
    assert caught.value.code == "cnpj_degenerate"


@pytest.mark.parametrize("digit", "0123456789")
def test_every_repeated_digit_cpf_passes_the_arithmetic_and_is_still_refused(
    digit: str,
) -> None:
    # Given a repeated-digit CPF
    cpf = digit * CPF_LENGTH

    # When its check digits are computed from its own base
    # Then the arithmetic agrees — this is true of ALL TEN of them, which is why the
    # explicit rule is not optional for CPF
    assert cpf_check_digits(cpf[:9]) == cpf[9:]

    # And yet the validator refuses it
    with pytest.raises(ValidationError) as caught:
        validate_cpf(cpf)
    assert caught.value.code == "cpf_degenerate"


# --------------------------------------------------------------------------------
# CPF
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("cpf", VALID_CPFS)
def test_valid_cpfs_are_accepted_masked_or_not(cpf: str) -> None:
    # Given a valid CPF
    masked = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"

    # When it arrives with or without its mask
    # Then both are accepted and normalize identically
    validate_cpf(cpf)
    validate_cpf(masked)
    assert normalize_document(masked) == cpf


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("1114447773", "cpf_length"),
        ("111444777355", "cpf_length"),
        ("1114447773A", "cpf_characters"),
        ("ABCDEFGHIJK", "cpf_characters"),
        ("11144477734", "cpf_check_digits"),
        ("00000000000", "cpf_degenerate"),
    ],
)
def test_cpf_rejections_report_their_own_reason(value: str, code: str) -> None:
    # Given an input that is wrong in exactly one way
    # When it is validated
    with pytest.raises(ValidationError) as caught:
        validate_cpf(value)

    # Then the reason is specific
    assert caught.value.code == code


def test_cpf_refuses_letters_even_though_cnpj_accepts_them() -> None:
    """CPF did NOT go alphanumeric. Only CNPJ did.

    IN RFB nº 2.229/2024 changes the CNPJ. Accepting letters in a CPF because the
    CNPJ validator does would silently widen a field that the Receita has not widened.
    """
    # Given a CPF-shaped value containing a letter
    # When it is validated
    with pytest.raises(ValidationError) as caught:
        validate_cpf("1114447773A")

    # Then it is refused for having an impermissible character
    assert caught.value.code == "cpf_characters"

    # While the same letter is entirely ordinary inside a CNPJ
    validate_cnpj(OFFICIAL_CNPJ)


def test_the_cpf_weights_are_not_the_cnpj_weights() -> None:
    """CPF's weights do not restart at 9, and using CNPJ's cycle gives wrong digits.

    Recorded as a test because "reuse the CNPJ generator for CPF" is the obvious
    simplification and it is wrong.
    """
    # Given a CPF base whose correct first check digit is known
    base = "111444777"

    # When the CPF generator runs
    # Then it produces the documented digits
    assert cpf_check_digits(base) == "35"

    # And the CNPJ generator, applied to the same base, does not
    assert cnpj_check_digits(base) != cpf_check_digits(base)
