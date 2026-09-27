# Supplement tracking and phased protocols

Status: researched product proposal. Supplement support is now requested scope, replacing the earlier decision to postpone supplement dosing. Implementation is tracked in Beads and must preserve the existing explicit-review and deterministic-calculation rules.

## Recommendation

Add supplements as a separate, local-first ledger linked to nutrition reporting. Do not model a planned supplement as a food or count a scheduled dose as consumed. The design has four independent facts:

1. an immutable, user-confirmed product-label version;
2. an optional regimen containing one or more dated phases;
3. actual taken or skipped dose events;
4. deterministic daily totals split into food, supplement and combined contributions.

This supports ordinary vitamins, protein powders and multi-ingredient products as well as phased protocols such as creatine loading. It also prevents a plan, reminder or AI interpretation from silently becoming intake.

## Telegram UX

Keep exact logging fast:

```text
creatine 5 g
took my creatine
magnesium 200 mg
/supplements today
/supplements plans
```

The first implementation slice uses these deliberately explicit forms while the
natural-language and photo draft parser is still pending:

```text
creatine 5 g
I took 5000 mg creatine
/supplement product add Plain Creatine | 5 g | 1 scoop | aliases: my creatine
/supplement take 1 serving | my creatine
/supplement edit S1r1 3 g
```

The product command is itself the label review and authorization: every field is
visible in the message that creates the immutable snapshot. A later OCR flow must
use a separate preview and approval button; it cannot write through this direct path.

`took my creatine` resolves directly only when one active plan and one exact reviewed serving are unambiguous. Otherwise the bot presents a draft. Every new or OCR-derived label and every new regimen requires an explicit confirmation button before it affects totals or reminders.

Suggested commands and buttons:

- `/supplement add` — search NIH DSLD or enter/photograph a label, review, then save a private product version.
- `/supplement plan` — create a daily, as-needed or phased regimen.
- `/supplements today` — planned, taken, skipped and remaining doses plus ingredient totals.
- `/supplements history` — recent actual doses and corrections.
- Receipt buttons: **Edit**, **Delete**, **Undo**, **Skip planned dose**.
- Plan buttons: **Approve plan**, **Change dose**, **Change times**, **Pause**, **Stop**.

Exact product aliases such as `my creatine` are private shortcuts. Saying only `I took magnesium` without a known product records no invented amount; the bot asks for the product or can retain an explicitly unquantified event that does not enter nutrient totals.

## Creatine template

The default setup should be **steady daily creatine monohydrate** because loading is optional and the simple path reduces logging and gastrointestinal burden:

- user selects an exact 3–5 g daily amount;
- timing is a user-chosen habit anchor, because evidence does not establish a meaningful pre- versus post-workout advantage;
- no cycling or tapering is invented.

An advanced **loading then maintenance** template may offer:

- fixed loading: 20 g/day as four 5 g doses for a user-selected 5–7 days;
- optional weight-based loading: 0.3 g/kg/day, calculated from a displayed and frozen starting-weight snapshot, then divided into practical reviewed servings;
- maintenance: a user-selected 3–5 g/day after loading.

The plan preview must show dates, daily total, number and size of servings, product version, calculation source and the maintenance transition. Phase changes are calendar based. Absence of a log remains unconfirmed; only a user action marks a dose skipped. The bot does not double the next dose, extend loading, or restart loading automatically. Because there is no direct missed-dose trial, that behavior must be labelled a conservative product rule based on muscle stores changing over weeks.

Starting creatine, entering loading, leaving loading, stopping or restarting creates a weight-trend context event. Adaptive calorie proposals should be withheld when one of those events falls inside their weight-analysis window; the bot must not classify acute scale gain as fat gain. The event is explanatory, not proof that any particular weight change came from water.

Evidence basis:

- NIH identifies creatine monohydrate as the most studied form and describes loading followed by maintenance. [NIH ODS exercise supplement fact sheet](https://ods.od.nih.gov/factsheets/ExerciseAndAthleticPerformance-HealthProfessional/)
- Loading around 20 g/day for 5–7 days followed by 3–5 g/day is a common evidence-based protocol; loading is optional. [IOC consensus statement](https://pmc.ncbi.nlm.nih.gov/articles/PMC5867441/)
- A foundational trial found that 3 g/day for 28 days increased muscle creatine similarly, but more slowly, than 20 g/day for six days. [Hultman et al.](https://pubmed.ncbi.nlm.nih.gov/8828669/)
- Dividing loading doses is practical: one study reported more diarrhea from a single 10 g serving than from two 5 g servings. [Ostojic and Ahmetovic](https://pubmed.ncbi.nlm.nih.gov/18373286/)
- Current evidence does not establish an ideal workout-relative timing. [Timing review](https://pubmed.ncbi.nlm.nih.gov/34445003/)

## Data model

Use immutable versions and current pointers, matching foods, meals and training:

- `supplement_products`: stable identity, private alias, brand, product name, form, barcode and jurisdiction.
- `supplement_product_versions`: current pointer target, serving definition, label source, DSLD ID, label date, review state, content hash and source evidence.
- `supplement_ingredients`: canonical ingredient, printed label name, chemical form, exact/unknown amount, unit, comparison qualifier and printed `%DV`.
- `supplement_certifications`: certifier, program, exact product or lot, evidence URL and checked date; never inferred from a logo alone.
- `supplement_plans` and immutable revisions: product version, purpose text, active/paused/stopped state, user-selected versus clinician-directed provenance, timezone and effective dates.
- `supplement_plan_phases`: ordered name, start/end rule, daily target, serving size, frequency and optional reminder windows.
- `supplement_dose_events` and revisions: local date/time, exact amount, product version, phase snapshot, taken/skipped status and source message/action.
- `health_context_events`: creatine phase changes or other user-confirmed contexts relevant to interpreting weight trends.

Do not pre-generate an indefinite table of future doses. Derive due doses from the current immutable plan revision and persist only sent scheduler jobs and actual taken/skipped events. Historical doses retain the exact product and phase version even after a label or plan changes.

## Deterministic calculations

- Planned doses never contribute to intake.
- Confirmed doses scale the exact reviewed label serving.
- Daily and weekly views show `food`, `supplement` and `combined` nutrient amounts separately.
- Calories and macros in protein powders, oils or gummies count once from their reviewed label. Pure creatine is tracked as an active ingredient rather than assigned invented calories.
- Creatine and other nonessential actives appear in an exposure/adherence ledger, not micronutrient-gap scoring.
- Proprietary blends with undisclosed ingredient amounts remain unknown.
- `%DV` is display metadata, not the personal target. RDA, AI and UL values come from the selected versioned reference profile.
- UL applicability is data: total intake, supplement/fortified sources, or a particular chemical form. A UL is a limit, never a target.
- IU, RAE, DFE and other form-dependent conversions occur only through reviewed ingredient-specific conversion rules.
- Duplicate active ingredients across products produce a factual stack warning.

## Data sources and provider boundaries

Use the [NIH Dietary Supplement Label Database API](https://dsld.od.nih.gov/api-guide) as optional search/enrichment for U.S. labels, with the same preview, review, immutable selection and offline-cache model as USDA. DSLD is not comprehensive for Armenian or European products and a database entry is not proof of approval, efficacy or measured contents. Manual label entry and photo-assisted review remain first-class.

The FDA does not pre-approve supplements for safety and effectiveness and does not routinely assay each product before sale. The UI must describe saved values as label claims. [FDA supplement questions and answers](https://www.fda.gov/food/information-consumers-using-dietary-supplements/questions-and-answers-dietary-supplements)

Third-party certification metadata may help product choice, especially for competitive athletes, but is not an efficacy claim or guarantee. Record verifiable product/lot evidence and check date. [NSF Certified for Sport](https://www.nsf.org/consumer-resources/articles/certified-for-sport-program) and [USADA risk-reduction guidance](https://www.usada.org/substances/supplement-connect/reduce-risk-testing-positive-experiencing-adverse-health-effects/)

## Safety and AI boundary

The bot may show sourced protocol templates, label limits, duplicate ingredients and applicable nutrient-UL comparisons. It must not diagnose deficiency or toxicity, clear a supplement as safe for the user, replace prescribed treatment, or run an LLM-based interaction checker.

Before activating a protocol, use a short suitability gate for medication or condition concerns, kidney/liver disease, pregnancy/breastfeeding, planned surgery, under-18 use and clinician-directed restrictions. A positive answer pauses plan activation and asks the user to review it with a clinician or pharmacist. The gate is not a medical record or clearance test.

For creatine, also disclose that supplementation can influence serum creatinine and therefore interpretation of creatinine-based kidney estimates; the bot does not interpret laboratory values. Recent evidence found a modest creatinine increase without a significant GFR change across the included studies, which is reassuring for studied populations but does not establish suitability for kidney disease. [2025 systematic review and meta-analysis](https://pubmed.ncbi.nlm.nih.gov/41199218/) and [KDIGO 2024 CKD guideline](https://kdigo.org/wp-content/uploads/2024/03/KDIGO-2024-CKD-Guideline.pdf)

AI may parse a message or label image into a draft and explain already-computed results. It may not invent quantities, choose a dose, approve a product, infer certification, decide an interaction or commit a regimen. All authoritative arithmetic and phase transitions remain local and deterministic.

## Implementation order

1. Product/ingredient versions, manual label review, exact dose ledger and correction history.
2. Generic plan phases plus the reviewed creatine steady/loading templates and weight-context integration.
3. Food/supplement/combined nutrient reporting with source-specific UL logic and duplicate-stack checks.
4. Scheduler reminders and taken/skipped actions using the existing timezone/settings design.
5. Optional DSLD lookup/cache, then photo/OCR drafts through the existing replaceable AI boundary.

Postpone inventory counts, automatic purchasing, broad drug-interaction databases, biomarker interpretation, autonomous protocol selection, causal performance claims and automatic dose changes from weight or training data.

## Implemented calendar planner

The local planner adds immutable previews and resolutions at schema
`0020_supplement_plans`. `/supplement plan` explains the exact setup forms:

```text
/supplement plan steady YYYY-MM-DD 5
/supplement plan loading YYYY-MM-DD 7 5
/supplement plan weight YYYY-MM-DD 7 5 80
```

The fields are start date, loading days (when applicable), maintenance grams,
and explicit starting kilograms (weight option only). The Telegram approval
button confirms the displayed dates, gram amounts, frozen weight, timezone,
reference version and the stated suitability concerns. Cancelling writes no
regimen. Plans currently use measured creatine monohydrate grams; no product
serving is inferred. Replace a plan by stopping it and approving a new preview.

`/supplement plans` displays up to three recent plans and today's dose slots.
`/supplement dose R1r1 1 taken` logs the displayed amount only when the user
confirms it was consumed. `skipped` records no intake. `unconfirmed` clears the
mark and deletes its linked intake through the existing revision ledger; a new
mark can then correct it. Separate direct intake logs are never matched to slots
automatically, and the help warns against marking an already logged dose again.
Dose marking currently supports today; independent exact logs support intake
outside the plan flow. The original plan timezone defines the dose date.

Pause/resume/stop commands require the current R…r… reference. Resume preserves
all original calendar dates; stopping is final. Dose absence cannot extend a
phase or increase a later dose. Scheduled loading-end context is derived from
the approved plan; start, pause/stop and resume context is stored immutably.
These contexts conservatively withhold calorie adjustments across the full
28-day weight-analysis window, including approval of an older pending calorie
proposal. They do not assert that a dose was taken or that water caused a
particular weight change. Reminders remain a separate Beads task. Daily/weekly combined nutrient and
adherence reports are now available; see [supplement reporting](supplement-reporting.md).
