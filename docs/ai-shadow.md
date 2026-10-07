# Shadow classification engineering boundary

This is an offline synthetic harness and private shadow runner, not an AI product
feature. Nothing registers an admin or view for AgentRun or EvalRun, changes
deterministic extraction/review, or executes model-proposed effects.

## Closed by default

`AI_PROVIDER` is empty, `AI_GLOBAL_DISABLED=true`, and
`AI_REAL_DATA_APPROVED=false`. A tenant must independently have `ai_shadow`.
The candidate `openai` adapter is **not** an owner selection or DPA approval.
OD-05 remains OPEN. Do not enable real data until the owner clears provider,
no-training/retention terms and transfer requirements. There is no fallback.

`fake` uses no network. The candidate adapter sends one bounded HTTPS request to
the fixed provider host, with `store=false`, structured output, no tools, no
redirects and no retry. Its protocol reference is
https://platform.openai.com/docs/api-reference/chat/create.
`AI_MODEL`, `AI_API_KEY`, `AI_INPUT_BRL_PER_MILLION` and
`AI_OUTPUT_BRL_PER_MILLION` are required for a real call. Rates must be conservative
BRL/FX ceilings supplied by the operator, not guessed prices.

## Admission and accounting

Only clean, classified/linked documents with an EvidenceLink are eligible.
Divergent, quarantined and unscanned sources are refused both before enqueue and
at admission. Empty text, missing/multiple CNPJs produce a local abstention.
The output parser rejects extra properties, duplicate keys, other client
identifiers and URLs. Prompt delimiters are not the security boundary.

Each attempt reserves R$0.025 in AgentRun before I/O. Admission holds a PostgreSQL
transaction advisory lock while checking those durable records: at most two
attempts and R$0.05 per source hash/client; defaults of 100 calls and R$5 per
tenant's Sao Paulo calendar day; R$50 globally per calendar month.
Every tenant's totals, including inactive firms, are read under its own RLS
context. This linear-in-tenant-count pilot implementation favors a correct cap
over a separate cross-tenant usage ledger.

Reservations are conservative, not invoiced cost. They are never refunded,
including when the provider fails or the worker is interrupted. `cost_brl` is
reported usage; sub-cent Decimal precision prevents rounding calls to free.
Pending reservations cannot be reclaimed automatically: redelivery skips them.
The deterministic path is already committed before enqueue; broker and provider
failures cannot roll it back.

T22's UsageRecord is deliberately not recreated here. Placement adds the
`ai_shadow_calls` usage write after that model exists; admission does not depend
on it.

## Content and retention

AgentRun is portal-NONE, tenant-isolated and client/document-paired. No raw prompt,
document text or failed provider response is persisted in traces. Validated
output is `ai_derived`: expired content is redacted after 365 days, and document
deletion clears it. Budget metadata remains so deletion cannot reset caps.
Retention visits firms even after `ai_shadow` is removed; this is cleanup, not
feature rollout. EvalRun contains only platform-level synthetic metrics.

## Offline evaluation

```sh
uv run python manage.py build_eval_set
uv run python manage.py run_eval --model fake --set v1
```

The committed set contains 25 specimens in each of 12 strata: product-invoice XML,
service-invoice XML, DAS PDF, bank-statement PDF text, receipts, blank scanned
PDF placeholders, wrong-company, two-CNPJ, alphanumeric CNPJ, poor text, injection,
and empty. All names/text/numbers are synthetic test examples. Labels are generated
independently of FakeLLM. FakeLLM metrics prove harness arithmetic and containment,
**not real-model accuracy**.

The wrong-client upper bound is the exact **one-sided 95% Clopper-Pearson** bound:
zero observed in 300 gives about 0.994%, not proof of zero population risk.
Per-field precision/recall, abstention correctness, injection outcomes, fixture or
measured latency, and cost are emitted. Partner review-time comparison stays null
until measured. A real-model gate record is not created by a fake evaluation.
