# Measured meals in Telegram

The local application now supports measured meal logging, food/quantity/date corrections, soft deletion and undo. It uses previously selected or manually reviewed local food versions. No AI or external food request occurs while processing a meal. The food catalog must contain the appropriate records first; see [USDA lookup](food-sources.md) and [manual import](food-catalog.md).

This covers the food ledger, daily totals and nutrient-data coverage. [Persistent drafts and explicit estimate approval](meal-drafts.md), aliases, favorites and recipes build on it. [Goals, weight trends and reports](goals-and-reports.md) add historical targets, completeness controls, weekly aggregation and adaptive calorie proposals. Training logs are also deployed; remaining-day meal suggestions remain upcoming.

## Log a meal

Find saved food records:

```text
/foods rice
```

The bot lists exact names, preparation states and food-version numbers. Use the matching version with measured edible weight:

```text
/meal Lunch: 180g #27; 250g #31
```

Here `#27` and `#31` illustrate numbers returned by the catalog, not preloaded food IDs. Exact catalog names also work:

```text
I ate 180g rice and 250g chicken
/meal yesterday Dinner: 150g rice; 200g chicken
```

Every named food must match a complete, unique saved name. Similar names, different preparations and unresolved foods produce choices and save nothing. Use a version number when a name contains ambiguous grammar such as “and,” or when several records have the same name. Raw/cooked preparation must be established in the source record. There is no automatic cooked/raw conversion.

The measured parser accepts g, kg and mg, including food-name-then-weight forms. Each quantity must resolve exactly to milligrams. It rejects ranges, volumes, counts and partial interpretations. A separate draft parser handles explicit rough masses or quantities missing from otherwise recognized foods. Up to ten foods fit in one meal. The complete meal is validated before saving; one unresolved ingredient prevents all of it from being committed.

“Three eggs” and “a cup of rice” still need clarification. “About 150g rice” creates an approval draft. An unweighed food with an exact catalog match creates a draft with either a clearly labelled local portion suggestion or an unresolved amount. Drafts stay outside totals. A weight written in grams never becomes measured merely because an estimate was expressed numerically.

The receipt shows its meal/revision reference, local date, foods, weights, preparation and source-version numbers. Energy/macros/fiber are calculated from stored source values. Missing values are shown as unknown; partial totals show known contributions and the number of unknown items. Display rounding is coarser than the stored calculation.

## Correct a receipt

Tap **Edit**, or reply directly to the latest meal receipt:

| Reply | Effect |
|---|---|
| `item 1: 120g` | Changes only the first item's quantity, keeping its original food version |
| `rice was 120g` | Changes one item whose full recorded name is exactly “rice” |
| `item 2: 80g #32` | Changes the second item's food and quantity |
| `item 2: delete` | Removes that item from a multi-item meal |
| `replace: 120g rice; 80g chicken` | Explicitly replaces the whole meal's item list |
| `date yesterday` | Moves the meal to the requested date without recalculating its nutrients |
| `date 2026-01-15` | Uses an explicit past date |
| `delete` | Removes the meal from the active diary while retaining revision history |
| `undo` | Restores the state before the latest change |

For a one-item meal, replying `120g` is sufficient to change its weight. For multiple items, use an item number rather than an ambiguous quantity. Use a separate date correction instead of embedding a date or label in item replacement text; otherwise the request is rejected rather than partly applied.

Delete and Undo are also inline buttons. Undoing the initial save marks the meal deleted. Undoing a deletion restores the prior exact snapshot. A completed undo cannot be undone repeatedly; use Edit for another correction. A deleted meal can be restored through an explicit replacement/edit. Every change produces a fresh receipt.

Use `/meals` to list the ten most recent meals, including deleted ones, and `/meal M7` to open the current receipt for meal 7. Explicit mutation commands require the revision in the reference:

```text
/edit M7r3 item 1: 120g
/delete M7r3
/undo M7r4
```

An old reference, old button or old receipt reply cannot overwrite a newer revision. Button/reply ownership, delivered-message identity and retention expiry are rechecked. An unrecognized reply is never treated as a new meal; send a standalone message for new consumption.

Editing the original Telegram message does not append or silently rewrite a meal. The bot returns the current receipt so the requested correction can be applied explicitly. Deleting a message in Telegram is not diary deletion.

## Daily totals

Request a report without any goal/profile setup:

```text
/today
/today short
how am I doing today?
show today's totals
/today yesterday
/today 2026-01-15 short
```

The default full report shows energy, protein, carbohydrate, fat, fiber, sodium, potassium, calcium, magnesium, iron, zinc, vitamin D, vitamin B12 and vitamin C. A short report shows energy, macros and fiber with a link to the same date's full report. Both include nutrient-data coverage, source counts and portion methods. Full reports additionally show nutrient-value origins and meal references. When a reviewed goal is active, reports show that date's historical calorie/macro target and remaining amounts. Food suggestions and configured micronutrient reference targets remain later work.

For each nutrient, coverage counts food entries with a known value, including known zeros. Repeated foods in separate entries each count. An absent nutrient row and a stored unknown both remain unknown. For example, `Fiber: 1.5 g known · data 1/2 foods` means one of two logged foods supplied fiber data; the amount is only its known contribution. It does not mean the day's full fiber intake was 1.5 g. An empty day says intake is unknown, rather than displaying zero intake. Coverage never measures whether all meals were logged or diagnoses a deficiency.

Amounts use the stored consumed nutrient snapshots, with no provider lookup, AI call, or recalculation from the current food catalog. Only current, sealed, non-deleted meal revisions count. A new report immediately reflects corrections, date moves, deletion and undo. Already sent reports are historical responses and are not edited in Telegram. Reports are read-only for the meal ledger; requesting one in reply to a meal receipt does not edit the meal.

Source categories distinguish reviewed manual records from USDA records. Known-value quality preserves `manual_reviewed` and `source_reported`; neither claims a laboratory measurement of the meal. Portion methods count measured and approved-estimate entries separately. Both report views disclose approximate contributions whenever approved estimates are included.

The aggregate includes every registered nutrient and every active food entry on the selected date. To fit one Telegram message, the full view displays all 14 core nutrients, up to six registered extensions, and up to five meal references. With long Unicode labels or extreme numbers, it keeps all core nutrients and reduces optional rows. Any omitted extension or meal rows are explicitly counted; omitted display rows never remove entries from calculated totals. A future extended-nutrient browsing view can expose more than this bounded summary. Nutrient values round only for display; positive amounts that would round to zero display as `<1 kcal` or `<0.1` of the canonical unit.

`/today` chooses the original request message's date in the profile timezone (or configured timezone before onboarding), even if processing is delayed. A historical query selects stored calendar dates; changing timezone never reassigns older meals. Explicit dates may be today or in the past. Midnight, daylight-saving changes and repeated local hours follow IANA timezone rules. Malformed requests and future dates produce help without changing the diary.

## Dates and persistence

A default meal date comes from the original Telegram message time in the profile's IANA timezone, falling back to configured timezone before onboarding. This keeps delayed/replayed messages on their original day. Explicit dates may be today or in the past; future meal dates are rejected.

Normal logs use message time as their default timestamp. Date-only backdated or moved meals use local noon as a deterministic storage anchor; this is not a claim that the food was eaten at noon. Each revision stores its timezone and local calendar date. Later timezone configuration does not move historical meals automatically.

Migration `0004_meal_ledger` adds four tables:

- `meals`: stable identity, source-message uniqueness and the current revision pointer.
- `meal_revisions`: immutable revision chain, owning action, date/time, label and deletion state.
- `meal_items`: measured amount, original units, exact food version, source identity and copied provenance.
- `meal_item_nutrients`: consumed nutrient quantities in integer millionths of the canonical unit, with explicit unknowns and source quality.

Source nutrient amounts are scaled with Decimal and quantized once at the consumed-snapshot boundary. Editing a quantity keeps the old food version unless a different version is explicitly chosen. Date changes, deletion and undo copy historical snapshots exactly. Refreshing the food catalog never rewrites saved meals.

The authorized action, meal revision, receipt and callback acknowledgement commit in the same transaction. User-facing validation failures roll back the nested meal operation; storage failures roll back the complete action for safe retry. Update/action/source-message uniqueness prevents duplicate meal creation after restart or replay. Outgoing Telegram replies can still be duplicated after an ambiguous network send, as documented in [foundation operations](foundation-operations.md).

Database guards prevent updates/replacement of sealed revisions and snapshots, invalid revision-pointer transitions, and deletion of food versions referenced by meal history. Soft deletion is reversible and does not erase personal data. Future permanent erasure must remove the complete meal history and associated retained messages; no permanent-erasure command is exposed here.

Raw message/receipt payloads retain the configured cleanup period (30 days by default). Structured meals remain until explicitly erased. Expired receipts require opening a fresh receipt from `/meals`. This increment does not add automatic backups or complete production recovery tooling.

Daily totals are calculated on demand and are not a second authoritative stored total. The aggregate uses Python integer sums so valid consumed amounts cannot overflow a SQLite integer sum. It preserves known/unknown counts, per-nutrient value origins, quantity methods and recorded timezones for future reporting. T04.1 advances the schema to `0006_meal_drafts`; migrations preserve measured history and add explicit approval provenance and temporary drafts.

## Verification

The T03 ledger milestone passed **476 local tests**; current combined draft verification is recorded in [meal drafts](meal-drafts.md). Ruff checks/format and strict mypy pass. Meal-specific coverage includes exact parsing, full-input rejection, consumed-nutrient rounding, unknown values, source-version preservation, quantity/food/date corrections, deletion/undo, source-message/update/action deduplication, receipt ownership/expiry/staleness, original-message edits, rollback after a simulated post-write validation failure, Unicode food-name matching, and safe rejection of unrepresentable dates. Migration tests preserve old data and guards; database tests cover sealed snapshots, replacement operations, revision transitions and complete-history deletion. Daily tests cover exact totals, absence versus null versus zero, repeated foods, source/quantity coverage, sums exceeding SQLite int64, full/short reports, bounded Unicode rendering, local midnight/DST, timezone changes, access control and query replay without ledger mutation.

Source and wheel builds pass, and release archive inventory excludes private and local task-tracker files. An installed wheel outside the source checkout passed a synthetic offline sequence: log → deliver receipt with buttons → reply correction → restart/replay → daily report → consistent backup/restore with database integrity and foreign-key checks. Restored daily totals and unknown micronutrients match the saved diary.

These diary paths have local, synthetic Telegram and Linux-container coverage. Basic live Telegram transport is verified, while full real-device acceptance of the diary and newer goal/report flows remains pending. Automatic off-site backup and complete production recovery remain upcoming tasks.
