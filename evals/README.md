# Synthetic evaluation seed

`parsing_cases.jsonl` contains public-safe synthetic cases, not actual user records. These are labeled requirements for the future model-evaluation runner and application integration tests. No runner or live evaluation exists yet.

Each line has `id`, `task`, `input`, optional `context`, `expected`, and `critical`. Expectations are declarative and do not prescribe final application DTO names. The evaluator should map real validated output to these semantics. Fields not present in `expected` are unconstrained except for the project's global approval, privacy, and data-integrity rules.

Cases combine parsing expectations with end-to-end checks. For example, a model may correctly interpret a rough quantity while only the application can enforce the approval button. Score extraction separately from the application behavior; never credit a model for a guarantee provided by a validator or database constraint.

Context uses synthetic identifiers and fixed reference dates. Do not infer that a known food has complete nutrient data, that a planned workout happened, or that a free-text statement is an authorized estimate-approval callback. For actions, authorization and revision checks are application tests rather than model judgments.

Before promotion, expand the set to include held-out text cases and reviewed image fixtures as described in `../docs/model-selection.md`. Image data must be synthetic or appropriately licensed; real owner photos remain private. The seed includes no image-quality benchmark and must not be presented as a vision evaluation.

A future report should contain model/provider/prompt/schema versions, per-case outcomes, schema-validity rate, exact field accuracy, clarification accuracy, critical violations, total measured cost, and median/p95 latency. Never run paid requests in normal PR CI. Synthetic input reduces privacy exposure; it does not exempt a route from the configured provider policy or budget.
