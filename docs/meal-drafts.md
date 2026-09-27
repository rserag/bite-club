# Meal drafts and explicit estimate approval

T04.1 is implemented and deployed. It extends the measured diary with persistent quantity drafts, conservative local portion suggestions, correction drafts and exact-revision Telegram approval. No AI, external food request, or paid API is needed. Container verification has passed; full real-device acceptance of this newer flow remains pending.

## Logging with uncertain quantities

The food must have an exact, unambiguous match in the reviewed local catalog. Preparation and food-version selection follow the [meal diary](meal-diary.md).

```text
about 150g rice
I ate about 150g rice and 200g chicken
/meal yesterday Dinner: roughly 180g #27; 250g #31
rice
```

The examples use hypothetical saved names/version numbers. Recognized approximation prefixes are `about`, `around`, `approximately`, `approx`, `roughly`, `estimated`, and `~`. Supported masses are g, kg and mg. Counts, volumes, ranges, unknown foods and unsupported qualifiers are not silently interpreted. [Favorites and repeats](meal-reuse.md) create fresh drafts whenever any reused portion is estimated. More flexible AI parsing and photos remain separate features.

The bot displays a draft reference such as `D1r1`, date, food/preparation/version, proposed amount and basis for each estimate. Measured entries are labelled separately. It offers **Enter amount**, **Approve estimate**, and **Cancel**. Drafts contribute nothing to totals.

If an amount is missing, the local suggestion policy is:

1. For the exact food version, use the median of the latest 3–10 qualifying measured meals. Repeated occurrences within a meal are summed; that meal is excluded if any occurrence of the food was estimated. Only active current revisions count. The result is rounded to whole grams, with a 1 mg minimum, and remains an estimate.
2. Without sufficient history, use a catalog portion only when exactly one documented portion is available. Its recorded mass and source/label are retained. Even a catalog portion marked precise is an estimate when selected implicitly for an unreported amount.
3. Otherwise leave the amount unresolved and offer Enter amount and Cancel. No default gram amount is invented and no approval button is available yet.

Prior approval does not authorize a future estimate. Estimated entries are excluded from measured-history suggestions, avoiding a feedback loop in which guesses become supporting observations. Changing a catalog record never changes the food version selected by an existing draft.

## Resolve, edit, resume or cancel

Reply to the draft receipt:

| Input | Result |
|---|---|
| `item 1: 120g` | Supplies a measured amount for that item |
| `item 1: about 120g` | Updates the estimate and creates a fresh draft revision |
| `item 2: 80g #32` | Changes that item's food and amount |
| `item 2: delete` | Removes an item from a multi-item draft |
| `replace: 120g rice; about 200g chicken` | Replaces the whole proposed meal |
| `date yesterday` | Moves the proposed meal date and invalidates old approvals |
| `cancel` | Discards the draft without changing saved meals |

For a one-item draft, `120g` is sufficient. If every item has a user-supplied measured amount, the bot saves it immediately. Resolving one item never approves estimates on other items. Words such as `yes`, `ok`, `approve` or `save` cannot approve an estimate; the button tap is required.

`/drafts` lists up to ten open drafts without extending their activity deadline. `/draft D1` reopens one. An explicit edit needs its current revision, for example `/draft D1r2 item 1: 120g`; `/cancel D1r2` cancels it. Old receipts/buttons show the current draft or its terminal state and never apply a stale change. Editing the original Telegram message displays the current draft without silently changing it. Normal query commands such as `/today` still work when sent as a reply to a draft.

Drafts survive process restarts. Opening or successfully editing a current draft refreshes its activity time. Background retries, listings and stale actions do not. After seven days without activity, opening, editing and approval are refused even if periodic cleanup has not run yet.

## Correct an already saved meal

Reply to the meal receipt with a rough correction such as `item 1: about 120g`. The bot creates a correction draft and keeps the saved meal unchanged until resolution or approval. If the saved meal changes while the draft is open, the old draft cannot overwrite it; open the latest meal and start the correction again.

A measured correction to a different item preserves unchanged approved estimates and their original approval metadata. Changing an estimate to a user-supplied measured amount clears its estimate metadata. Date changes, deletion and undo preserve historical snapshots. Newly introduced or changed estimates require fresh approval.

## Approval and storage guarantees

- Approval is tied to the exact draft ID/revision, itemized food versions and masses, authorized owner/chat, delivered Telegram receipt, opaque button token and allowed action. Replayed updates, repeated callbacks and buttons from another receipt cannot create duplicate meals.
- Creating/updating the meal, closing the draft and queuing its receipt occur in one local transaction. A rejected change rolls back the proposed mutation. Recognized draft references remain associated for privacy cleanup even when the edit was rejected.
- Each approved estimated meal item stores its method, basis, approving action, time, draft ID and draft revision. Receipts and both daily report views retain the estimated distinction. An approved estimate is not a measured quantity.
- Drafts reference immutable local food versions, protecting those versions while the draft is open. Nutrition is calculated deterministically only from source values; missing nutrients remain unknown.
- One-message previews display all proposed item amounts and version IDs, with bounded food-name/basis labels. Ten-item Unicode previews and saved receipts stay within Telegram's message size limit.

Migration `0005_approved_estimates` extends immutable meal snapshots while preserving existing nutrient data and SQL guards. Its downgrade refuses to discard any approved-estimate history. Migration `0006_meal_drafts` adds temporary draft records, protected food-version references and action associations. The migration rebuild has a rollback test with an injected interruption. Stop the worker, take a consistent backup and migrate before running the new release; see the deployment runbook. There is no automatic deployment in this increment.

## Retention

Saving, cancelling or expiring a draft removes its structured proposal and releases its temporary food references. Closing invalidates queued draft previews. Expiry purges associated local raw inputs, rejected edits, list/preview outputs and quoted copies. Only minimal identifiers and fixed safe status text remain for recognizing old buttons. Raw receipt references can still be associated for cleanup after their ordinary raw-retention period has passed. Saved meal snapshots, approvals and meal receipts follow their own history/retention rules.

Expiry is checked before processing callbacks, before claiming queued replies and in periodic cleanup. A message already in flight to Telegram cannot be retracted by local cleanup, but its old button cannot authorize an expired or changed draft. These guarantees concern local application data; Telegram copies and historical backups follow their own retention policies.

## Verification

The draft milestone passed **733 tests**; the current combined suite is larger and recorded in [foundation operations](foundation-operations.md). Coverage includes parsing, source/history selection, unknown amounts, approval binding, authorization, stale and duplicate actions, measured resolution, corrections, undo, restart persistence, expiry, rejected-input cleanup, Unicode message limits and migration rollback. Ruff checks/format and strict mypy pass. Source/wheel builds pass, and an installed wheel outside the checkout passed draft creation → restart → approval → replay rejection → daily totals → synthetic backup/restore with approval provenance and integrity checks. Release archives exclude private configuration and local tracker state. The feature is deployed with Linux-container coverage; full real-device acceptance remains pending.
