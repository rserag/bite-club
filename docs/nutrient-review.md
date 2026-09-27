# Reviewed nutrient references and intake screening

Start with `/nutrients references`. This installs the bundled, versioned reference set once and lists its available groups. Installation selects no personal profile. Use the returned set number explicitly:

```text
/nutrients values 1 male-31-50
/nutrients review 1 male-31-50
/nutrients review 1 female-51-70 2026-09-20
```

The group applies only to that request. The example set number is not guaranteed; use the number returned by the bot. Values, footnotes, source links and paginated continuation commands remain inspectable. Each review identifies its reference version. Existing sealed versions are never overwritten; a conflicting bundled version fails its integrity check.

The review covers the two Monday–Sunday weeks ending on the last completed Sunday. An explicit date must also be a past Sunday. The current partial week is excluded. Stored meal dates and current sealed revisions determine inclusion; edits/deletes and unresolved drafts invalidate food completeness through the existing check-in rules. All 14 day statuses and the two complete-day denominators are displayed.

## Reference provenance and scope

The bundled `adult-dri-2026-09-27-v1` is a supported-nutrient subset of US/Canadian DRIs, transcribed and checked against Health Canada's published tables on 27 September 2026. These are dietary references, not food-label Daily Values or individual prescriptions. The content hash covers the version, scope and all values/footnotes. The existing immutable reference tables store the set; no new schema migration or network fetch occurs at runtime.

Eight explicit categories cover male/female reference groups aged 19–30, 31–50, 51–70 and 71+. They apply to apparently healthy, non-pregnant, non-lactating adults with a typical mixed diet, using non-smoker vitamin C references. The bot does not infer age, reference category, pregnancy, menstrual status, smoking, diet pattern or clinical suitability. Different needs require an individually reviewed reference. In particular, the iron categories retain the source's menstrual assumptions; dietary totals do not establish absorption or blood status.

The bundle includes the ten non-energy nutrients already supported by the food registry:

| Source | Included values |
| --- | --- |
| [DRI elements tables](https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables/reference-values-elements.html) | Calcium, magnesium, iron and zinc RDAs/ULs; potassium AI; sodium AI and 2019 CDRR stored as `limit` |
| [DRI vitamin tables](https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables/reference-values-vitamins.html) | Vitamins C and D RDAs/ULs; vitamin B12 RDA |
| [DRI macronutrient tables](https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables/reference-values-macronutrients.html) | Total fiber AI |

Magnesium's UL applies to supplemental/pharmacological intake, excluding food and water. Sodium's CDRR is neither a UL nor a minimum to fill. No numerical UL is invented for nutrients without one. Form-sensitive nutrients outside the existing registry, such as vitamin A and folate, are not introduced through guessed conversions.

## Screening rule

A possible intake gap is shown only for a supported RDA when **both** weeks independently have at least five complete food days and their recorded daily averages are below 80% of that RDA. This percentage is a product reminder threshold, not a clinical deficiency threshold. Exact integer comparisons avoid rounding errors at the boundary.

At least 90% of consumed food entries must have known, non-imputed nutrient values in each week. Entry count is a disclosed quality heuristic, not a measure of nutrient importance. Any remaining unknown/imputed food may be a material source; because this ledger has no defensible upper bound for that contribution, it conservatively withholds the gap message even above 90%. The report names those foods. Unknown supplement quantities or conversions also block a gap assessment. A high known subtotal never becomes an adequacy or safety claim.

Food, supplement and combined recorded subtotals are separate. Imputed/other-quality food amounts appear separately and do not enter the screening total. Explicitly completed empty food days can contribute zero; unlogged days cannot. Incomplete days are excluded from both food and supplement averages so the denominator is consistent. Only actual current taken-dose snapshots contribute; plans, skipped slots and creatine are not micronutrient intake. Lack of supplement records means none logged, not confirmed non-use. Approved portion estimates retain their visible uncertainty.

AI, sodium, limits and UL values never generate gap messages or amounts to fill. Daily UL comparisons remain in `/supplements today|week ... reference <set> <group>`; weekly averages do not establish daily safety. No supplement recommendation, dose advice, deficiency diagnosis or blood-status inference follows from this report.

## Verification

Domain and database tests cover source distinctions, exact boundaries, missing/imputed values, blank versus completed-empty dates, eligibility in each week, immutable reference installation and actual meal/supplement corrections. The focused Telegram scenario installs/lists references, checks blank history, logs ten synthetic food days, marks them complete via buttons, follows pagination and verifies that missing micronutrients do not become inferred gaps:

```sh
uv run --group e2e python -m tools.telegram_e2e.cli run nutrients
```

The scenario uses the dedicated test bot and disposable database. The full `regression` suite includes it. This feature does not deploy local changes to production.
