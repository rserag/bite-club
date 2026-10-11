# Optional AI meal drafts

For day-to-day logging, see the [AI user guide](ai-user-guide.md). Operators can
review the private [AI efficiency measurements](ai-measurements.md) after two or
three days of ordinary use.

Local measured logging, favorites, recipes and reports continue to work without an AI provider. External interpretation is disabled until its own credentials and current policy configuration are present. No shared Codex credential file is read, no credits are purchased, and no paid evaluation runs in CI.

`AI_TEXT_ENABLED` and `AI_PHOTO_ENABLED` are independent runtime gates and both
default to `false`. Selecting `LLM_PROVIDER=chatgpt` or `openrouter` does not
enable either feature. Set each flag only after its own quality and privacy
review. `/status` shows each route separately. A disabled or failed subscription
route never silently switches to OpenRouter.

AI interprets unfamiliar meal wording or one meal photo into a proposal using reviewed local food identities. Unknown foods, preparation ambiguity and unaccounted-for ingredients produce clarification rather than a partial saved meal. Models cannot return nutrient values, database queries or executable actions. Authoritative nutrition still comes from immutable reviewed food versions and deterministic calculations. Every AI proposal requires the current displayed draft's approval button, including after quantities are edited into measured amounts. Old buttons and text replies cannot approve a new revision.

## ChatGPT plan sign-in

The optional preferred route uses Bite Club's own **Continue with ChatGPT** grant. Eligible account access and model availability are determined by OpenAI; the existing Codex session is independent. [Open-source registration](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [account sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions).

Run the local app-owned sign-in command:

```sh
uv run python -m nutrition_bot.adapters.ai.chatgpt_auth signin \
  --credentials private/chatgpt-credentials.json --open-browser
```

The listener binds only to `127.0.0.1` and uses an exact `/auth/callback` URI. Each attempt has fresh PKCE, state and OIDC nonce. The app uses the issued client identifier returned after registration, validates the ID token's signature against OpenAI's JWKS plus issuer/audience/expiry/nonce, and preserves a selected account's verified identity before replacing credentials. Rotating renewals retain the credential lock and finish bounded identity validation plus atomic persistence before propagating a caller cancellation. Tokens are saved atomically in an owner-controlled directory with `0700` permissions and a `0600` file. Granted scopes must include plan usage; identity sign-in alone does not enable inference.

Create a separate stable host identifier on a self-hosted VM before securely importing the protected credential record. The import command keeps the VM's own host identifier, and the VM subsequently owns rotating token refreshes. Never continue refreshing the transferred session simultaneously from the laptop. [Self-hosted VM procedure](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms).

```sh
python -m nutrition_bot.adapters.ai.chatgpt_auth host-id \
  --credentials /data/private/chatgpt-credentials.json
python -m nutrition_bot.adapters.ai.chatgpt_auth import \
  --source /data/private/imported-chatgpt.json \
  --credentials /data/private/chatgpt-credentials.json
```

Run these commands as the account that owns the bot runtime, with a writable private credential directory mounted separately from public source. Transfer over SSH, never through chat, public source or GitHub secrets. Delete the temporary import copy afterward.

A private `ChatGPTPlanPolicy` file must explicitly enable the route, confirm the account's training opt-out, contain a current dated policy review (at most 30 days), state the disclosed retention limitations, and select exact model slugs from that account's current catalog. Photo activation additionally needs a reviewed image-capable model. There is no universal claim of zero retention. The policy defaults to disabled; it never modifies the user's ChatGPT privacy settings by itself.

Requests verify the account's public model catalog, use only `https://api.openai.com/v1/responses`, and send `store:false`, `stream:true`, explicit context and no tools. Only a terminal `response.completed` event with a complete locally validated intent is accepted. When the preview's terminal output is empty, completed `response.output_item.done` snapshots are assembled by unique contiguous output indices; partial deltas, tool items, conflicting terminal output and events after completion are rejected. [Responses stream events](https://developers.openai.com/api/reference/resources/responses/streaming-events). The preview rejects `temperature` and `max_output_tokens`, so this integration makes no promise of a server-enforced output-token cap. A 30-second attempt timeout and bounded response bytes protect this application, while actual plan usage follows OpenAI account limits. [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).

The app counts subscription invocations in two independently enforced daily
pools: **bot use** and **validation/evaluation**. In the private policy,
`daily_invocation_limit` sets the bot cap and
`daily_evaluation_invocation_limit` sets the validation cap. Both default to 100:

```json
{
  "daily_invocation_limit": 100,
  "daily_evaluation_invocation_limit": 100
}
```

This is a policy fragment, not a complete activation policy. The pools use UTC
calendar days and reset their available allowance at **00:00 UTC**. Validation
calls cannot consume the app's bot pool; unused allowance cannot be borrowed
from the other pool. Existing invocation history is preserved when upgrading,
including earlier validation calls. If a pool already has 100 attempts today,
it remains exhausted until the next UTC day.

A separately authorized evaluation supplement can be configured for exactly one
UTC day using paired `extra_evaluation_date` and
`extra_evaluation_invocation_limit` fields. The cumulative extra count is bounded at350;
it changes only that day's evaluation cap, leaves the bot and base evaluation
caps unchanged, and expires automatically on every other UTC date. For example,
this synthetic private-policy fragment grants200 extra attempts on its stated
day:

```json
{
  "extra_evaluation_date": "2026-01-15",
  "extra_evaluation_invocation_limit": 200
}
```

Use it only for explicitly authorized supplements. When an additional supplement
is authorized for the same day, add it to the already authorized extra count;
do not replace existing usage or reset its ledger. Existing completed, failed
and unknown attempts remain counted; changing the policy does not reset usage or
authorize retrying an unknown invocation. Read-only cap reporting uses the same
frozen UTC day for the effective limit and usage window. A dated supplement is
separate from provider availability and never authorizes paid API evaluation.

These are application limits, not separate OpenAI account quotas or guaranteed
provider capacity. Both pools still use the same selected ChatGPT account and
share that account's actual provider limits. An account-level limit can block
either pool even while its app allowance remains. The counts are not dollar
charges. Failed, interrupted and unknown attempts stay accounted for in their
original pool and are not automatically repeated. Plan requests never debit the
OpenRouter dollar ledger. OpenRouter remains an independently configured
option; there is no automatic paid fallback from a failed ChatGPT request.

Run bot traffic and validations against the **same durable accounting database**.
Selecting a new or empty database is not a quota reset or an additional
allowance: it only hides the prior accounting. Once credentials have been
transferred for activation, the VM owns their rotating refreshes; use the
operator's VM procedure rather than reusing that session concurrently on the
laptop. The read-only [measurement report](ai-measurements.md) can show each
pool's current UTC-day use and, with an explicitly selected policy file, its
configured cap and remaining app allowance.

## OpenRouter configuration

The alternate paid route requires a dedicated OpenRouter key and a versioned endpoint manifest. The initial provider allowlist is empty. Candidate model names in [model-selection.md](model-selection.md) are not activated endpoints.

For each task, copy an exact model and provider endpoint slug from current official endpoint metadata. Record the policy/pricing source URLs, review and expiry dates, prohibition on training, disclosed retention purpose/duration, modalities, parameters and integer price ceilings. Photo routes also require a documented safe maximum image-token bound. Unknown capabilities/prices/policies or a review older than 30 days make the route ineligible.

The account review must confirm training opt-out, optional gateway content logging disabled, a dedicated key and automatic top-up disabled. Provider-side key usage limits add protection. The application does not purchase credits or change these account settings. [Provider logging](https://openrouter.ai/docs/guides/privacy/provider-logging), [routing controls](https://openrouter.ai/docs/guides/routing/provider-selection).

Each request pins one reviewed provider with `only`, disables fallback routing, requires parameter support, applies maximum pricing and requests strict JSON schema. Returned model/provider identities, token limits, cost and schema are checked locally. Policy, price or usage violations disable that route until the operator reviews and replaces its identifier. Neither `data_collection=allow` nor a provider display name alone establishes a no-training policy.

Before each attempt, reserve its conservative maximum in integer micro-USD. Shared paid usage cannot exceed $10 per monthly period. The first activation freezes the diary timezone's local-month boundaries as UTC timestamps. A timezone change cannot reset an active period or move its historical costs; later periods never overlap. Billing from interrupted or ambiguous responses stays reserved until trustworthy reconciliation. No response means unknown cost, never zero. At most two attempts are allowed, and the second is limited to a schema-only failure with known billing and its own full reservation. Clarifications, policy failures and ambiguous charges are not retried.

## Photos and retention

Send one Telegram meal photo outside an album. Downloads and actual bytes are
bounded to 2 MB, JPEG/PNG and 1600 pixels per side. The image is sent only to an
activated route and remains in memory during interpretation; local raw photo
copies are not archived. Include food names and preparation in the caption so
only relevant reviewed catalog identities are sent. Without usable caption
context, the app asks for manual details before inference. This release does
not perform a second model call to retrieve an image-only catalog.

A photograph cannot establish edible grams. Numeric quantities must match a
verbatim portion statement in the input naming that food. Approximate statements
remain estimates; absent quantities stay unresolved. Visual guesses and
unsupported conversions from counts, cups, skin or bone weight are discarded.
Identity/preparation ambiguity and unaccounted ingredients require clarification.
An unknown identity can continue through the existing discovery/review workflow
with the complete proposed meal and its date retained. Source review and the
latest meal proposal approval remain separate steps.

Displayed and saved draft provenance uses fixed application wording; model-written labels are limited to canonical meal names and model-written explanation prose is discarded, so nutrient claims cannot hide in those fields. AI accounting stores no prompts, raw model responses or photo bytes. Temporary structured outcomes are consumed in the same transaction that creates a draft or processes a rejection; crash-left outcomes are purged after seven days. Existing inbox/draft retention also applies. Historical accounting retains only minimal request IDs, route IDs, attempt status, timestamps and costs. This does not govern Telegram or provider copies.

Before a sole worker starts, `AiService.recover_abandoned()` marks interrupted requests as unknown, retains their reserved spending/invocation counts and prevents duplicate inference. Do not run that recovery against a different active worker. Application networking always occurs outside database write transactions.

## Synthetic evaluation

The implemented opt-in runner validates synthetic fixture files without external
calls unless `--live` is supplied. The current representative suites and explicit
per-route thresholds are in [the evaluation guide](../evals/README.md): 54 text
cases and 20 photo cases using ten distinct generated, visually reviewed images.
The older blank-photo refusals remain regression seeds and cannot activate vision.

```sh
uv run python -m nutrition_bot.application.ai_evaluation \
  --cases evals/ai_meal_review_cases.jsonl --run-id synthetic-review
```

Candidate comparisons can select `--split development` and `--max-cases N`;
held-out cases remain for the final unchanged full suite. A live run additionally
requires `--live`, an owner-only private `--report` path, and either the app-owned
`--chatgpt-credentials`/`--chatgpt-policy` pair or an explicitly authorized
OpenRouter `--manifest` and dedicated environment key. The runner preserves
per-case results and resumes the same snapshot/configuration without repeat
inference. It uses the existing migrated durable accounting database and never
saves meals or approves drafts. Quota, budget, disabled and unknown outcomes stop
the bounded run and remain failures in its report.

Each OpenRouter attempt counts against the shared monthly budget and a persistent
maximum of $1 for that evaluation run identifier; this engineering ceiling is
not authorization for paid evaluation. ChatGPT evaluations count against the
same account's daily validation pool, including failed and unknown attempts.
Reports show separate role quality gates and separate raw typed model versus
normalized application scores for catalog/quantity/date extraction, completeness,
uncertainty and clarification/discovery, plus latency and known/unknown usage.
Application clamping cannot give the model credit for invented grams or lost
uncertainty. Raw typed proposals exist only transiently in the evaluation service
and in the owner-only synthetic report; production metrics never retain them. Passing mocked tests or an incomplete pilot does not activate either route.
