# Initial model assignments and evaluation

Prepared 21 September 2026. These are implementation candidates, not measured winners or activated production endpoints. No paid inference or evaluation has been run. Provider availability and prices must be rechecked before enabling live requests.

## Proposed assignments

| Task | Initial candidate | Listed input / output USD per million tokens | Why evaluate it |
|---|---|---|---|
| Unfamiliar meal/workout text, intent extraction, historical-query classification | `google/gemini-3.1-flash-lite` | $0.25 / $1.50 | Economical extraction candidate with image input and JSON-schema output support |
| Meal photos and nutrition-label extraction | `google/gemini-3.6-flash` | $0.75 / $3.75 | A separate multimodal candidate for visual interpretation; validate performance on actual task examples |
| Complex explanations across computed nutrition/weight/training facts | `anthropic/claude-sonnet-4.6` | $3 / $15 | Higher-cost explanation candidate used selectively, compared against Flash on identical supplied facts |

Sources: [Flash Lite listing](https://openrouter.ai/google/gemini-3.1-flash-lite), [Flash listing](https://openrouter.ai/google/gemini-3.6-flash), [Sonnet listing](https://openrouter.ai/anthropic/claude-sonnet-4.6). Each listing reports structured outputs and image inputs. Endpoint compatibility still needs verification. Prices are listed standard rates, not guaranteed quotes; image tokenization, reasoning, retries, provider tiers, credit-purchase fees, and taxes can affect actual cost. Do not use floating `latest`, automatic model-choice, or free-model aliases as production assignments.

Compare `google/gemini-3.5-flash-lite` as a challenger for routine text. Its listed rates are $0.30 / $2.50 per million tokens, so promote it only if the task evaluation justifies the higher cost. [Listing](https://openrouter.ai/google/gemini-3.5-flash-lite)

This shortlist favors explicit, available model identifiers over a claim of choosing the newest or universally best model. Newer models may replace candidates after the same evaluation. The three application roles remain stable even if two roles eventually share a model.

## What goes to a model

- Local parsing has first chance for known commands, aliases, measured templates, and unambiguous short forms. It must account for meaningful qualifiers or decline.
- Text parsing receives the current input and only relevant food aliases/draft context. It returns typed intents, unresolved fields, and assumptions, not nutrition facts or executable queries.
- Vision receives a bounded-size image and the extraction schema. Photos yield candidate foods/quantities; labels yield transcribed values and units with a review requirement. A legible label is source evidence, not permission to invent omitted nutrients.
- Historical questions select predefined query types. Application code retrieves and calculates facts; the explanation model receives that result and source identifiers. Render authoritative numeric tables from those results, not from generated prose.
- Routine daily/weekly reports have deterministic renderers. A complex explanation can use the stronger role without making the basic report depend on it.
- No autonomous agent loops, web search, external tools, SQL execution, image generation, or full-history upload.

## Endpoint privacy and routing

The confirmed policy allows reviewed temporary retention and prohibits training on submitted data. Disable prompt-training permission for both paid and free routes in the OpenRouter account, leave optional OpenRouter content logging off, and pin a reviewed provider allowlist per model. OpenRouter documents separate account controls for training and provider retention. [Provider policies](https://openrouter.ai/docs/guides/privacy/provider-logging)

Create a versioned endpoint manifest with model ID, exact provider slug, policy source URL, review date, training prohibition, disclosed retention purpose/duration, input modalities, supported parameters, and price ceiling. Candidate listings are not approval of every upstream endpoint. Resolve provider slugs from current endpoint metadata; do not guess them from display names. Start with the allowlist empty until reviewed; local functionality remains usable.

Require JSON-schema parameter support and use provider `only` restrictions. Fallbacks must satisfy the same manifest. Do not assume `data_collection=allow` alone implements a no-training policy: its documented scope can permit training. A stricter `deny` filter may exclude otherwise acceptable temporary-retention routes. Account training opt-out plus the reviewed allowlist is the baseline for the chosen policy; test actual routing behavior before release. [Routing controls](https://openrouter.ai/docs/guides/routing/provider-selection)

If a policy, price, or capability becomes unknown, disable that route. Do not silently broaden provider selection. Do not misrepresent these settings as control over Telegram copies, external legal retention, or the physical absence of transient caching.

## Limits and failure handling

Engineering starting limits, configurable after evaluation: text input up to 8,000 tokens with 1,500 generated tokens; photo/label up to 12,000 token-equivalent input with 2,000 generated tokens; complex explanations up to 12,000 input with 2,500 generated tokens. These are ceilings, not targets. Cap image count/size and account for provider-specific image tokenization; if a safe reservation cannot be calculated, do not send the request. Include billed reasoning in the generation limit/reservation.

Use one active paid request at a time initially, a 30-second attempt timeout, and at most two attempts per logical request. Retry only eligible transient/schema failures; neither ambiguous human intent nor an unclear photo is solved by repeatedly spending. A higher-cost retry is permitted only when explicitly configured for that task and fully budget-reserved. Otherwise offer clarification/manual entry.

Store monetary amounts as integer micro-USD. Reserve a conservative maximum cost before each attempt in a transaction, reconcile returned usage, and retain unresolved reservations after ambiguous timeouts until reconciled. Never release a reservation merely because the client lost its response. Both retries count. A stale/unknown price disables that paid route. The $10 cap covers this bot's usage only; dedicate an OpenRouter key and use provider-side limits as an additional control. Account auto-top-up stays off.

## Evaluation and promotion

Seed cases are in `../evals/parsing_cases.jsonl`; the runner is an implementation task, not an existing command. Extend to at least 40 text cases, 20 explicitly labeled image/label cases, and 10 historical-question cases before production. Use synthetic data or privately supplied, authorized images; never commit real meal history/photos. Include readable/blurred labels, per-serving/per-100g changes, decimal commas, mixed units, cooked/raw ambiguity, correction references, rough-template reuse, and embedded malicious instructions.

Measure schema validity, exact intent/field extraction, appropriate clarification, unsupported nutrition claims, numeric preservation, latency, token use, total cost, and provider route. Mask personal data in saved outputs. Use a held-out portion for model/prompt changes.

Promotion gates: zero unauthorized saves, estimate approvals, policy escapes, or invented numeric facts reaching authoritative output in the integration suite; at least 95% correct fields/intent on clear-input fixtures; all designated ambiguity cases ask for the missing fact or preserve uncertainty. Treat these thresholds as release gates for the test set, not proof of real-world accuracy. Vision reviews and user approval remain required even when evaluations pass.

Mocked evaluations run in CI. Live evaluations are opt-in, separately bounded (suggested initial ceiling $1, within the monthly budget), and require an actual configured key/approved manifest. If a candidate fails, compare a challenger or adjust the prompt/schema and rerun the affected held-out cases; do not remove difficult cases to make it pass.
