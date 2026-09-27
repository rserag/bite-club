# Supplement nutrition and adherence reports

Use `/supplements today` for a daily comparison and `/supplements week` for the seven calendar dates ending today. The singular `/supplement` spelling also works. Add a date to inspect recorded history:

```text
/supplements today 2026-09-22
/supplements week 2026-09-22
```

Each nutrient row separates **F** (food), **S** (logged supplement contributions) and **C** (combined recorded amounts). Food coverage and the number of days explicitly marked complete remain visible. Known partial sums are labelled; unlogged days are not turned into zero. A date explicitly marked as all food logged can have zero food intake. That food marker says nothing about supplement-log completeness.

Actual current, sealed, non-deleted taken-dose snapshots contribute once. Skipped slots, unconfirmed slots, plan previews and approved schedules do not contribute. Editing, deleting or restoring an intake changes the report through its current revision; old product-label versions remain the source of old intake. Stored local dates are preserved. Weekly values are recorded sums, not complete-day averages or inferred intake.

Creatine and other non-nutrient actives appear in a separate exposure section. Pure creatine is not assigned invented calories or treated as a micronutrient deficiency. An exact elemental amount can be known even when its chemical form is unknown. Proprietary blends, non-exact label quantities and unreviewed unit conversions remain unknown for exact nutrient totals. No IU, RAE or DFE conversion is guessed.

When two distinct products contribute the same ingredient on the same recorded date, the report lists that factual overlap. Repeated doses of one product and separate dates are not described as a duplicate product stack. This does not assess interactions or establish safety.

## Plan adherence

For each regimen, the report reconstructs calendar phases and active intervals from immutable revisions, in the plan's original timezone. It counts scheduled slots on dates active for any part of the day, then shows taken, skipped and unconfirmed slots. A plan paused or stopped later does not erase earlier scheduled days. A wholly paused day adds no scheduled slots, and a loading-to-maintenance transition retains its original calendar date.

Only explicit dose marks link intake to a slot. A separate direct creatine log contributes to exposure but is not automatically matched to a plan. A deleted linked intake returns that slot to unconfirmed; an edited amount remains taken but is distinguished from the planned amount. Current-day unconfirmed slots may still be taken later; they are not labelled missed. No catch-up dose, automatic loading extension or adherence percentage is inferred.

## Optional reviewed upper limits

`/supplements references` lists installed, sealed reference sets and their groups. Select a reviewed group explicitly for one report:

```text
/supplements today reference <set-number> <group>
/supplements week 2026-09-22 reference <set-number> "group with spaces"
```

Selection is per request; no population, reference set or group is selected automatically. If no reviewed reference is installed or selected, the report explicitly says upper limits were not assessed. Use `/nutrients references` to install/list the bundled adult DRI set; see [nutrient review](nutrient-review.md) for provenance, applicability and two-week intake screening. General nutrient-product authoring remains separate from the current creatine-only Telegram catalog. The reporting engine reads mapped nutrient components when available and is tested with synthetic labels and reference values.

Only values tagged **UL** are used for these comparisons. RDA, AI, other limits and label %DV are not treated as upper limits. Every comparison identifies the selected set/version, group, source, eligible sources and any required chemical form. Weekly reports check each day independently; they do not compare a seven-day total or weekly average with a daily limit.

- Supplement-only ULs exclude food.
- Total-intake ULs combine eligible food and supplement amounts.
- Food form and fortified fractions are not recorded in the present food snapshots. Form-specific or fortified-source comparisons therefore retain that uncertainty instead of treating every food value as eligible.
- Unknown forms/amounts and unavailable conversions produce an indeterminate comparison unless the known eligible amount already exceeds the limit. A reviewed lower-bound quantity can establish exceedance; it cannot establish an exact total.
- A recorded subtotal below a UL is never a safety clearance. Unlogged intake and individual suitability are not assessed.

The distinction follows the source-specific nature of nutrient references: the NIH [magnesium fact sheet](https://ods.od.nih.gov/factsheets/Magnesium-HealthProfessional/) distinguishes supplemental from food magnesium for the UL, and the [folate fact sheet](https://ods.od.nih.gov/factsheets/Folate-HealthProfessional/) distinguishes synthetic forms and sources. The broader [NIH reference overview](https://ods.od.nih.gov/HealthInformation/nutrientrecommendations.aspx) distinguishes ULs from recommended intakes. The application does not diagnose deficiency or toxicity.

## Long reports and verification

Reports paginate within Telegram's message limit. Follow the `More:` command in the footer; it preserves the requested dates and reference group. Pages are fresh read-only queries of the current ledger, so a correction made between requests is reflected on the next page.

The focused live suite is:

```sh
uv run --group e2e python -m tools.telegram_e2e.cli run supplements
```

It uses only the dedicated test bot and disposable local databases. It exercises actual Telegram product logging, duplicate-product reporting, delete/undo, approval of a synthetic plan, taken/skipped/cleared slots and independent direct intake. The broader `regression` suite includes these scenarios. Database and domain tests cover source/form-specific ULs, unknown quantities, immutable label snapshots, large exact sums, Unicode pagination and historical/timezone adherence boundaries.
