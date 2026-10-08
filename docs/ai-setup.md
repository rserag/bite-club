# Optional AI meal drafts

Local measured logging, favorites, recipes and reports continue to work without an AI provider. External interpretation is disabled until its own credentials and current policy configuration are present. No shared Codex credential file is read, no credits are purchased, and no paid evaluation runs in CI.

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

The app separately counts subscription invocations, with an operator-configured maximum of 100 per UTC day. This count is not a dollar charge. Failed or interrupted attempts consume that invocation allowance and are not automatically repeated. Plan requests never debit the OpenRouter dollar ledger. OpenRouter remains an independently configured option; there is no automatic paid fallback from a failed ChatGPT request.

## OpenRouter configuration

The alternate paid route requires a dedicated OpenRouter key and a versioned endpoint manifest. The initial provider allowlist is empty. Candidate model names in [model-selection.md](model-selection.md) are not activated endpoints.

For each task, copy an exact model and provider endpoint slug from current official endpoint metadata. Record the policy/pricing source URLs, review and expiry dates, prohibition on training, disclosed retention purpose/duration, modalities, parameters and integer price ceilings. Photo routes also require a documented safe maximum image-token bound. Unknown capabilities/prices/policies or a review older than 30 days make the route ineligible.

The account review must confirm training opt-out, optional gateway content logging disabled, a dedicated key and automatic top-up disabled. Provider-side key usage limits add protection. The application does not purchase credits or change these account settings. [Provider logging](https://openrouter.ai/docs/guides/privacy/provider-logging), [routing controls](https://openrouter.ai/docs/guides/routing/provider-selection).

Each request pins one reviewed provider with `only`, disables fallback routing, requires parameter support, applies maximum pricing and requests strict JSON schema. Returned model/provider identities, token limits, cost and schema are checked locally. Policy, price or usage violations disable that route until the operator reviews and replaces its identifier. Neither `data_collection=allow` nor a provider display name alone establishes a no-training policy.

Before each attempt, reserve its conservative maximum in integer micro-USD. Shared paid usage cannot exceed $10 per monthly period. The first activation freezes the diary timezone's local-month boundaries as UTC timestamps. A timezone change cannot reset an active period or move its historical costs; later periods never overlap. Billing from interrupted or ambiguous responses stays reserved until trustworthy reconciliation. No response means unknown cost, never zero. At most two attempts are allowed, and the second is limited to a schema-only failure with known billing and its own full reservation. Clarifications, policy failures and ambiguous charges are not retried.

## Photos and retention

Send one Telegram meal photo outside an album. Downloads and actual bytes are bounded to 2 MB, JPEG/PNG and 1600 pixels per side. The image is sent only to an activated route and remains in memory during interpretation; this increment does not archive local raw photo copies. A photograph never creates authoritative nutrient data or proves a weighed portion. Unresolved portions remain unresolved; visible portions are proposals for explicit review.

AI accounting stores no prompts, raw model responses or photo bytes. Temporary structured outcomes are consumed in the same transaction that creates a draft or processes a rejection; crash-left outcomes are purged after seven days. Existing inbox/draft retention also applies. Historical accounting retains only minimal request IDs, route IDs, attempt status, timestamps and costs. This does not govern Telegram or provider copies.

Before a sole worker starts, `AiService.recover_abandoned()` marks interrupted requests as unknown, retains their reserved spending/invocation counts and prevents duplicate inference. Do not run that recovery against a different active worker. Application networking always occurs outside database write transactions.

## Synthetic evaluation

Mocked contract tests run offline in CI. A separate opt-in runner accepts only explicitly marked synthetic cases; it neither saves meals nor approves drafts. Input files supply synthetic catalogs and expected typed intents, never real meal history. Without `--live`, it only validates the fixture file and makes zero external requests. `--role meal_text` or `--role meal_photo` selects one group. Summary accuracy compares exact food-version IDs and integer milligrams, accepting numerically equivalent gram spellings while preserving the quantity threshold.

```sh
uv run python -m nutrition_bot.application.ai_evaluation \
  --cases evals/ai_meal_cases.jsonl --run-id synthetic-review
```

A live run additionally needs `--live` and either `--manifest` plus the dedicated key in its environment, or `--chatgpt-credentials` and `--chatgpt-policy`. It uses the existing application's migrated accounting database. Every OpenRouter attempt counts against both the shared monthly budget and a persistent maximum of $1 for that evaluation run identifier. ChatGPT evaluations count against the independent daily invocation quota. No live evaluation or endpoint promotion is implied by passing mocked tests; representative text and image accuracy must be reviewed before activating real inputs.
