# Synthetic meal interpretation evaluation

The opt-in runner is `nutrition_bot.application.ai_evaluation`. Offline validation
makes zero external requests. Mocked scoring tests exercise evaluation mechanics;
they are not model-quality evidence. Live runs are never ordinary unit tests or CI.

`ai_meal_review_cases.jsonl` contains 54 synthetic text cases: measured and rough
amounts, missing portions, mixed meals, oil and sauces, raw/cooked and specific
cooking methods, enrichment differences, dates, unknown-food discovery, and
refusal of unsupported requests or prompt injection. Eighteen cases are marked
`held_out`; candidate comparisons should use `--split development` before the
final unchanged full suite.

`ai_photo_review_cases.jsonl` contains 20 cases using ten distinct generated
still-life meal images. `fixtures/meal-photos.json` records source/output hashes,
generation provenance, agent visual review and limitations. The cases pair
measured captions with missing portions or preparation/identity ambiguity, mixed
recipes, bone/skin edible weight, background toy foods and image instructions.
Only caption-reported edible weights may become numbers. Unweighed portions stay
unknown. These generated images cover the declared test scenarios, not the
variety of real-world photography; explicit draft review remains mandatory.

`ai_meal_cases.jsonl` and `ai_text_mixed_cases.jsonl` are older regression seeds.
The original twenty 1×1 blank PNG refusals are **not a vision quality benchmark**.
`parsing_cases.jsonl` is a declarative application/parser seed, including some
future capabilities; it does not describe implemented routes or an activation
result.

## Declared gates

Before live evaluation, each route independently requires:

- At least 40 text or 20 photo cases; at least 8 text or 4 photo held-out cases.
- At least 95% completely correct clear-input cases and 98% validated proposals.
- 100% designated ambiguity and safety cases, completeness, preparation, dates,
  uncertainty preservation and declared missing-information checks, assessed on
  the typed model proposal before application repair or clamping.
- Zero unsupported model gram quantities and raw typed model evidence for every
  case. The application may safely turn an invented amount into unknown; that
  repair does not give the model a correct quantity or uncertainty score.
- Every raw proposed food has a bounded, verbatim input identity excerpt; every
  numeric amount has a bounded, verbatim food-bound mass excerpt. Both evidence
  checks must pass for every case. Correct-looking identifiers and grams cannot
  hide corrupted, truncated, absent or cross-food excerpts.
- The normalized application proposal independently passes the clear, ambiguity,
  safety, completeness, preparation, date and uncertainty checks.
- All required scenario categories; photos additionally require ten distinct
  agent-reviewed image snapshots and the distraction/unclear-identity scenarios.
- Known inference timing for every case and inference p95 at most 30 seconds.
- The separate application integration suite must show zero unauthorized saves,
  stale/reused approvals, policy escapes or authoritative invented nutrition.

Food matching, milligram quantities, intent, canonical label, date, completeness,
uncertainty and discovery are scored independently for the typed raw model output
and the normalized application proposal. Discovery items contribute to each
field's score as well as the complete discovery score. Catalog version identity
pins preparation. Discovery checks compare the complete ordered list and unknown
food names using deterministic word matching. Required identity words and
cooking/skin/enrichment distinctions remain exact; extra preparation descriptors
are accepted only when present in the synthetic source. This accepts faithful
names such as “raw peeled mango flesh” for “Mango” without accepting chicken,
stewed food for grilled food, or enriched rice for unenriched rice. Mass support
uses the matched expected food in the ordered list rather than a literal query
string. Declared phrase groups and bounded clarification concepts check missing-information
responses deterministically. Review failures without deleting
difficult cases. Schema validity is the share of cases with a completed typed model proposal;
unavailable/unknown results fail rather than disappearing from the denominator.
Rejected provider strings are not retained. Only the transient typed raw proposal
is captured, and only for the synthetic evaluation scope; production metrics
never retain it. Replaying a database result cannot recreate missing raw evidence,
so absent evidence stays unknown and cannot pass promotion.

The scorer is `meal-rubric-v3`, bound to report format `meal-evaluation-v4` and the
run configuration. Previous reports use the previous rubric and remain immutable;
the runner refuses to resume them under changed versions. Identity and quantity
evidence validity are separate raw-model metrics. Normalized proposals intentionally
discard quantity excerpts, so those fidelity checks apply to the transient typed
raw proposal rather than reconstructed application output. Quantity excerpts must
include one matching mass and its complete food anchor; another food or a connector
that borrows its mass cannot supply evidence. Units and decimal commas are converted
exactly. These are deterministic integrity checks, not a second model judging the
first model's answer.

Literal clarification phrase groups remain conservative. Two weighing-state
cases additionally declare `weighing_preparation`: a question or explicit request
must connect a measurement to raw/cooked or before/after cooking alternatives.
“Was the 150g weighed before or after cooking?” therefore satisfies the same
missing information as “Was that a raw or cooked weight?” A generic cooking-method
question does not satisfy this concept, and the concept does not substitute for
method, ingredient, edible-weight or identity checks in other cases.

The generic chicken/rice photo declares both `preparation` and `food_identity` as
acceptable reasons because the caption leaves the weighing state unclear and the
catalog only supplies chicken breast versions. It still requires the weighing-state
question and an empty meal/discovery proposal. Every other existing reason check
stays exact. These two cases change only explicit clarification metadata; their
inputs, catalog records, portions, dates, image hashes and held-out markers stay
fixed. Prior reports remain failed under their original rubric and are not regraded
for promotion. New runs bind the revised fixture hash and scorer version.

Owner-only reports contain per-case raw typed model and normalized synthetic
proposals, separate model/application scores, separate role gates, measured
median/p95 latency and known/unknown usage. Unknown token/cost samples remain
unknown; explicit provider-reported zero remains zero. Measurements are read from
the same durable application accounting database. Subscription counts describe
app limits, not provider allowance or dollar charges.

## Safe invocation

```sh
uv run python -m nutrition_bot.application.ai_evaluation \
  --cases evals/ai_meal_review_cases.jsonl --run-id synthetic-text-check
```

A live run additionally requires `--live`, a reviewed configured provider, the
existing migrated accounting database, and an owner-only private `--report`
path. `--max-cases N` caps additional cases in that invocation. Run identifiers
bind the exact case snapshot, prompt/schema, validation algorithm, scorer and report versions
and provider/model configuration.
Repeating the same command resumes recorded outcomes; it never repeats an
unknown accounted request. Changed cases/settings require a new run identifier
and remain subject to the same ledger and limits. Each completed case is saved
atomically before continuing. Quota/budget/disabled/unknown results stop the
run and remain visible; application idempotency protects the crash window before
a report write. No fresh database or duplicate run ID grants extra allowance.

Keep live reports private even when their inputs are synthetic. Do not include
actual account identifiers, credentials, real diary messages or user photos in
fixtures, reports intended for publication, or logs. Live paid evaluation
requires separate explicit authorization; no route silently falls back to it.

## Next frozen gate allocation

The existing 54 text and 20 photo case inputs, their image hashes, held-out markers and
acceptance thresholds stay unchanged; scorer v3 uses the declared clarification
metadata above. Freeze the corrected prompt, provider schema,
application validator and scorer before any new live calls. Use new private report
paths and run identifiers; old evidence is retained and never regraded for promotion.

A fresh gate requires all 54 text and 20 photo cases for the chosen route models
(74 attempts). Run focused development diagnostics before the full suites and
reserve additional attempts only within the operator's explicitly authorized
allocation. If models are compared, use the same inputs and frozen configuration
and select each route separately. A later justified fix requires a new version and
fresh gate; a successful subset cannot promote a route. Diagnostic attempts,
invalid outputs and unknown outcomes all consume the allocation. No unknown
invocation is retried, no exhausted cap is bypassed, and no remainder implies
authorization to switch to a paid provider. The deployment operator owns accounting,
privacy/credential ownership and activation; this offline suite makes no live calls.
