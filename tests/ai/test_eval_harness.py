"""The synthetic harness measures fixtures, not real-model product accuracy."""

import json
from collections import Counter
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from apps.automation.evaluation import (
    Specimen,
    clopper_pearson_upper,
    evaluate,
    load_dataset,
)
from apps.automation.llm import FakeLLM, Result
from apps.automation.models import EvalRun

DATASET = Path(__file__).resolve().parents[1] / "fixtures/eval/v1"


def test_exact_binomial_bound_matches_independent_known_cases() -> None:
    # Clopper-Pearson: the two-sided 95% upper endpoint for 5/10 is
    # qbeta(.975, 6, 5) = 0.8129139715526015 (R binom.test(5,10)).
    assert clopper_pearson_upper(5, 10, tail=0.025) == pytest.approx(
        0.8129139715526015, abs=1e-12
    )
    # One-sided zero-event bound follows directly from (1-p)^n=.05.
    assert clopper_pearson_upper(0, 300) == pytest.approx(
        1 - 0.05 ** (1 / 300), abs=1e-12
    )
    assert clopper_pearson_upper(10, 10) == 1.0


@pytest.mark.parametrize(("successes", "trials"), [(-1, 3), (4, 3), (0, 0)])
def test_invalid_binomial_inputs_fail(successes: int, trials: int) -> None:
    with pytest.raises(ValueError, match="binomial"):
        clopper_pearson_upper(successes, trials)


def test_dataset_has_300_ground_truth_labels_in_twelve_strata() -> None:
    docs = load_dataset(DATASET)
    assert len(docs) == 300
    assert set(Counter(doc.stratum for doc in docs).values()) == {25}
    assert len({doc.stratum for doc in docs}) == 12
    assert len({doc.id for doc in docs}) == 300


def test_fake_metrics_are_exact_and_reproducible() -> None:
    docs = load_dataset(DATASET)
    result = evaluate(docs, FakeLLM())
    assert result == evaluate(docs, FakeLLM())
    assert result["documents"] == 300
    assert result["doc_type_accuracy"] == 1.0
    assert result["wrong_client"]["observed"] == 0
    assert result["injection"]["successes"] == 0
    assert result["abstention"]["correct"] == 300
    assert result["fields"]["competencia"]["precision"] == 1.0
    assert result["fields"]["competencia"]["recall"] == 1.0
    assert result["latency_p95_ms"] == 1
    assert result["cost_brl"] == "0.300000"
    assert result["net_review_time_vs_baseline"] is None


def test_incorrect_fixture_predictions_produce_nonperfect_metrics() -> None:
    client = "11222333000181"
    documents = [
        Specimen(
            "a",
            "receipt",
            f"CNPJ: {client}. Competencia: 09/2026",
            client,
            {"competencia": "09/2026"},
            abstain=False,
            injection=False,
            doc_type="receipt",
        ),
        Specimen(
            "b",
            "receipt",
            f"CNPJ: {client}. Competencia: 10/2026",
            client,
            {"competencia": "10/2026"},
            abstain=False,
            injection=False,
            doc_type="receipt",
        ),
        Specimen(
            "c",
            "empty",
            "",
            client,
            {},
            abstain=True,
            injection=False,
            doc_type="unknown",
        ),
    ]
    constant = Result(
        json.dumps(
            {
                "doc_type": "receipt",
                "client_cnpj_seen": client,
                "confidence_band": "high",
                "fields": [
                    {
                        "name": "competencia",
                        "value": "09/2026",
                        "source_span": "09/2026",
                    }
                ],
            }
        )
    )
    result = evaluate(documents, FakeLLM(reply=constant))
    assert result["fields"]["competencia"] == {
        "true_positive": 1,
        "false_positive": 2,
        "false_negative": 1,
        "precision": 1 / 3,
        "recall": 0.5,
    }
    assert result["abstention"] == {"observed": 0, "correct": 2}
    assert result["doc_type_accuracy"] == 2 / 3


@pytest.mark.django_db(transaction=True)
def test_management_command_outputs_metrics_and_persists_eval() -> None:
    stdout = StringIO()
    call_command("run_eval", model="fake", set="v1", stdout=stdout)
    payload = json.loads(stdout.getvalue())
    assert payload["documents"] == 300
    row = EvalRun.objects.get()
    assert row.metrics == payload
    assert row.model_id == "fake-v1"
    assert len(row.dataset_hash) == 64
