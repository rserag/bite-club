# Goals, weight trends and adaptive calorie reviews

The deployed bot supports deterministic goal setup and reporting without an AI or nutrition API call. Container verification has passed; full real-device acceptance of these newer flows is still pending.

`/goal setup` shows the reviewed input formats. An estimated plan requires an explicit equation coefficient and total-activity factor; a manual plan accepts calorie, protein and fat targets directly. Both produce a Telegram proposal first. **Apply tomorrow** creates an immutable target-plan revision, while **Cancel** leaves targets unchanged. Carbohydrate is the deterministic calorie remainder. Estimated plans retain their reviewed calorie range; an adaptive change cannot move outside it.

`weight 79.4` or `/weight today 79.4 morning` records an immutable, correctable measurement. `/weight` reports a seven-calendar-day mean when at least four dates are measured and a robust Theil–Sen 28-day rate when at least seven dates span 14 days. One date contributes one daily point: an explicitly marked morning measurement is preferred, otherwise the date median is used. Missing dates are never interpolated.

`/today` shows the target effective on the selected historical date and the remaining calorie/macronutrient amounts supported by known data. The short view leads with recorded amounts and progress; Details holds routine source/coverage counts and definitions. Partial sums, unknown values and approximate portions remain explicit in either view. The dated question asks whether everything eaten was logged. **All food logged** and **Not all logged** create audited day-status events and share a button row separate from Details/navigation. Unresolved drafts remain outside totals and prevent completion; any later food-ledger change makes an older completion marker stale, so adaptation cannot rely on it. Training never adds estimated exercise calories back into the daily target.

`/week` produces the full seven-date report; `/week short` gives the concise form. Complete dates contribute to averages. Incomplete and unknown dates remain visible and are excluded instead of being treated as zero. The report uses each date's historical target and discloses nutrient coverage. Micronutrient gap screening remains withheld until a versioned personal reference set is implemented.

`/adjust` runs a local evidence review. It will not create a proposal unless all of these conditions hold:

- the current target has been stable for at least 21 days;
- the latest 28-day window contains at least 12 measured weight dates, spans at least 14 days, has at least three measurements in each of its three most recent seven-day blocks, and has no gap longer than seven days;
- at least 19 of the 21 food dates are explicitly complete, with no unresolved drafts and complete energy data;
- every complete date has a historical calorie target, and mean intake is within 10% of the mean target;
- a robust observed weight-change rate is available.

The deadband is the larger of 0.10 kg/week and 25% of the absolute intended rate. A mismatch outside that band must occur in the same direction in two eligible reviews exactly seven days apart. The calculation is deterministic:

```text
raw_delta = (target_rate − observed_rate) × 7700 / 7
proposal_delta = round_to_50(clamp(0.5 × raw_delta, −150, +150))
```

The result must also fit the reviewed calorie range and leave feasible carbohydrate targets. A pending receipt shows the evidence, proposed calorie change and resulting macros. **Apply tomorrow** appends one future-effective target plan; **Keep target** records the decision without changing targets; **Review evidence** reopens the same stored evidence. Buttons are tied to the authorized owner, delivered Telegram message and exact proposal. Repeated Apply presses return the existing result and cannot create another plan.

Adaptive review rows are immutable evidence snapshots. Proposals retain pending/applied/kept state and link an applied result to its target-plan revision. Daily and weekly aggregates remain calculated views; meals, food versions, weight revisions, completion events, goals, target plans and adaptive decisions are the permanent authoritative records.

The calculation is a conservative feedback controller over self-reported data, not a medical diagnosis or a precise metabolism measurement. Missing, sparse or internally inconsistent data produces an explanation and no target change.
