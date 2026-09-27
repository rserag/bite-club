# Batch recipes and cooked yield

T04.3 implements versioned recipes using reviewed ingredient data and deterministic portion calculations. It requires no AI or additional service. This increment is deployed with container verification; full real-device Telegram acceptance remains pending.

## Define a batch

Choose exact foods and preparation with `/foods`, then save ingredients and one denominator:

```text
/recipe create chili = 500g #12; 300g #18; about 20g #21 | yield 1200g
/recipe create breakfast batch = 400g #12; 600g #18 | servings 4
/recipes
/recipe R1
```

These food numbers are illustrative. Ingredients need explicit edible amounts in g, kg or mg; the existing exact catalog and personal-alias selection applies. A definition contains one to ten ingredients. Counts, missing amounts, nested recipes, meal dates, extra text and ambiguous foods are rejected. No usual-portion assumption fills an ingredient quantity.

Choose either total edible cooked yield or a declared number of equal servings. The bot never converts between them without a definition. Denominators accept explicit approximate wording, such as `yield about 1200g` or `servings about 4`. Ingredient preparation stays attached to its reviewed food version; no universal raw-to-cooked factor is invented.

Saving, viewing or changing a recipe records no food consumption. A recipe is a reusable recorded formulation and batch size, not an inventory of remaining food. A new cooked batch with a different yield needs a new version or a separate named recipe.

## Log a portion

```text
/recipe log R1v1 300g
/recipe log yesterday R2v1 1.5 servings
/recipe log R1v1 about 300g
```

Use an explicit version and a positive quantity no larger than the defined batch. Grams, kg and mg are accepted for yield recipes; servings are accepted for serving recipes. Input quantities must resolve exactly to integer milligrams or thousandths of a serving. Batch limits are 50 kg cooked yield or 1000 declared servings. There is no automatic default portion: **Enter portion** shows how to supply one.

A receipt displays the actual cooked portion or servings and its exact fraction of the batch. Beneath it, ingredient equivalents show calculated shares of the recorded ingredient masses. Those equivalents are not separately weighed food portions, and their sum is not the cooked portion's weight. Very small equivalents display as less than 0.001 g without rounding the underlying calculation to zero.

Nutrients come from the pinned ingredient records. Calculations assume a representative share of the batch; the application does not infer cooking losses, absorbed water nutrients, discarded fat or nutrient retention factors. Record ingredients and edible yield consistently. Unknown nutrients remain unknown and partial data stays partial; a missing micronutrient value never becomes zero.

## Estimates and corrections

A fully measured definition and exact reported portion can be logged immediately. Any estimated ingredient, denominator or consumed portion creates a fresh consumption draft. The draft identifies the batch version, portion and uncertainty. Estimates do not enter totals until the owner taps **Approve estimate** on the current draft revision.

Ingredient uncertainty affects that ingredient's contribution. Estimated yield or portion affects the whole portion. Saving an estimated recipe grants no future approval, and reusing an approved recipe meal never inherits its approval action or time. Entering an exact consumed portion cannot remove uncertainty in an ingredient or batch denominator.

Reply to a recipe meal or draft with:

```text
portion 250g
portion about 0.5 servings
date yesterday
```

A portion correction recalculates from the original pinned recipe version. A rough correction to a saved meal creates a new approval draft; the current meal remains unchanged until approval. If that meal changes meanwhile, the stale draft cannot overwrite it. Ordinary delete and undo preserve the original recipe context and nutrient snapshots.

Individual ingredient-weight edits on a consumption receipt are refused because those displayed amounts are calculated equivalents. Change the batch definition for future uses, or explicitly replace the entire logged meal with direct foods using `replace: ...`. This makes detachment from the recipe explicit. Updating a recipe never silently changes a saved meal or open consumption draft.

Favorites, `/repeat`, and `same again` preserve the recipe identity and exact fraction. Scaling changes the consumed portion, not the underlying ingredient batch weights. The result must remain within the defined batch and valid input precision. Any retained uncertainty still creates a new approval draft. Recipe equivalents never train the ordinary single-food measured-portion suggestion algorithm.

## Versions and archive controls

```text
/recipe update R1v1 = 500g #12; 350g #18 | yield 1300g
/recipe R1v1
/recipe log R1v1 300g
/recipe archive R1v2
/recipes all
/recipe restore R1v3
```

Updates, archiving and restoration require the current displayed version and create a new immutable version. Older controls cannot mutate a newer version. Explicitly selecting an older version for consumption is supported while the recipe is active, so leftovers can retain their original batch yield. Historical views say which version is selected. Archiving hides the recipe and blocks new direct recipe logs until restoration; it does not alter past meals or remove pinned favorites. Existing favorites and repeats retain their own explicit saved portions.

Recipe names occupy the `/recipe` namespace. They never override measured food logs, estimates, aliases or `/eat` favorite names. Viewing and **Enter portion** are read-only. Callback authorization, message binding and duplicate-action protection use the same durable transport as the rest of the diary.

## Calculation and storage contract

A portion is the exact rational ratio `portion_units / total_units`. Mass units are milligrams; serving units are thousandths. Each nutrient is calculated as:

```text
source_nutrient_per_100g × ingredient_batch_mg × portion_units
----------------------------------------------------------
                    100000 × total_units
```

The application uses integer arithmetic and rounds only the final stored nutrient amount to its existing millionth-unit scale, using half-even rounding. Equal thirds and sub-milligram ingredient shares work without inventing a weighed quantity. Separate portions can differ from a single whole-batch calculation by the final numerical quantization; no inventory or remainder-allocation service is introduced.

Migration `0009_recipes` adds recipe identities, immutable batch versions and pinned ingredient rows. Migration `0010_recipe_shares` adds typed recipe-share provenance to meal and favorite items, references to recipe versions, and temporary draft recipe references. The schema head is `0010_recipe_shares`.

**For items with recipe-share provenance, `edible_milligrams` stores the whole-batch ingredient mass.** The consumed amount is that mass multiplied by the stored exact fraction. All food versions, original batch masses, denominator, portion, recipe version, estimate bases and calculation version remain available. Direct food items retain their existing consumed-mass interpretation. Readers must use recipe-share provenance; treating a batch mass as a consumed mass would be incorrect. Totals continue to use immutable consumed nutrient snapshots.

The ledger validates recipe context against its stored immutable definition, preventing altered fractions, ingredient identities or uncertainty metadata from bypassing approval. Favorites and drafts carry the same context. Active references protect recipe versions; closing or expiring a draft releases its temporary reference. Draft cleanup removes raw quoted copies while preserving saved recipe views, favorites, meals and the independent lifetime of newer drafts.

## Operation and verification

There are no new external APIs, credentials, processes or ports. Personal recipe names, ingredients, meal history and backups stay outside Git and release archives. Stop the worker and take a consistent SQLite backup before migration. Migration adds provenance without rebuilding existing sealed meal guards. Downgrade refuses to discard recipe-bearing meal, favorite or draft data; restore a matching backup for rollback instead.

The complete local suite passes **1,171 tests**; Ruff checks/format and strict mypy pass. Offline verification covers exact batch fractions, immutable history, unknown and zero nutrients, per-use estimate approval, portion corrections, favorites/repeats, source pinning, callback authorization, retention, migrations and existing diary regressions. The packaged application passed acceptance outside the checkout, restart/replay, and synthetic backup/restore with exact recipe provenance, approval metadata and integrity checks. Its source and migrations match the current workspace; the allowlisted release excludes private and tracker state. Local verification is distinct from Linux deployment and live Telegram acceptance.
