# Reviewing AI efficiency

Bite Club records compact private measurements in its application database. They
include routing, outcomes, model and prompt/schema versions, elapsed times,
provider-reported token usage, and links to draft revisions. They contain no
message text, food names, photos, credentials, account IDs, URLs, or raw provider
responses. Internal request and draft keys support deduplication and linking;
the summary never exports them.

After two or three days of ordinary use, read the rolling window:

```sh
uv run nutrition-bot ai-metrics --days 3
```

In a running Compose deployment:

```sh
docker compose exec -T bot nutrition-bot ai-metrics --days 3
```

The command requires only the configured database path, accepts 1–30 days, and
prints aggregate JSON. It can run while the bot holds its normal worker lock. It
opens SQLite in read-only mode, makes no external calls, and does not expire drafts
or update any application state. Run the explicit migration before using it on an
older schema. Keep any saved output private.

## Reading the summary

- `sources.normal` and `sources.evaluation` separate everyday use from explicit
  synthetic model evaluations. An evaluation does not improve the real-use sample
  size.
- Normal input counts cover eligible original free-text messages and photos after
  local routing. Commands, replies, edited messages and callbacks are excluded.
  These counts are not all Telegram messages or a verified count of meals.
- `handling` shows local handling versus AI fallback. `statuses` separates usable
  drafts, clarification, disabled/quota/budget responses, and unavailable or unknown
  outcomes. `inference_sent` counts requests that sent inference;
  `attempts_sent` includes any bounded retries. Preflight failures can produce AI
  routing measurements without sending inference.
- `latency` shows elapsed handling time. `ai_stages` separates authentication,
  model-catalog lookup and inference. Times exclude the later Telegram delivery
  delay and time spent waiting for the user to review a draft. Median and p95 use
  known samples; p95 uses the nearest-rank method. Small samples need caution.
- `usage` reports known and unknown samples plus known token/cost totals for sent
  inference requests. A `null` total means no usage was known; a numeric total with
  unknown samples is partial. Explicit provider-reported zero remains zero.
  Reasoning tokens can be part of output tokens and cached tokens can be part of
  input tokens: do not add all token fields together.
- `subscription_invocations` reports the app's bounded invocation ledger for the
  full UTC days overlapping the rolling window, separating normal and evaluation
  use. The first day's count can include invocations before the rolling window
  began. `subscription_invocations_scope` makes this distinction explicit. It is
  not the provider account's
  remaining allowance or billing statement. Unknown or interrupted invocations
  remain accounted for.
- `drafts` reads current open/saved/cancelled/expired states for requests started
  in the selected window. Saved drafts are classified as unchanged or edited by
  comparing their final revision with the first displayed revision. An edit that
  was later reversed still counts as edited. Later corrections to a saved meal
  do not change this draft classification. Permanently removed linked drafts are
  reported as removed. Open drafts have not yet supplied a final outcome.
- `versions` records the model, prompt/schema versions and reasoning setting used
  for AI requests. `default` means the request omitted an explicit reasoning
  setting; it does not certify a particular provider default.

## Collecting a useful baseline

Keep the model, prompt, schema and reasoning settings stable during the first
collection period, and use the bot normally. Avoid adding diagnostic inference
calls solely to inflate the sample. If configuration changes, compare the recorded
version groups separately.

Measurements are optional and fail safely: replaying a request does not add a
second record, and a failed measurement write cannot prevent the enclosing meal
action. A static `ai_metric_unavailable` log event indicates a recording problem
without disclosing content. Hard process termination before a record is committed
can leave a measurement gap; invocation accounting remains a separate durable
source. If a request was denied before inference and the same key is later sent,
its unsent AI measurement upgrades once to the sent observation. Local records,
linked draft records and the first sent observation remain unchanged on replay.
Report sample sizes, known/unknown usage and unfinished drafts alongside
any efficiency conclusion.

After collecting the baseline, ask for a review of the last three days covering
local handling, latency, token usage, failures and draft corrections. Use the
measurements to choose one optimization, then compare the next stable period.
Passing accuracy and ambiguity checks remains a separate prerequisite for model
activation or changes.
