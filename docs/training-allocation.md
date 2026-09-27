# Reviewed training-day allocation

`/allocation` creates an immutable preview for a future Monday–Sunday week. It uses
one existing reviewed baseline target plan across all seven dates. The user supplies
the seven planned day types and chooses a redistribution step:

```text
/allocation YYYY-MM-DD rest gym bjj rest double unknown rest shift=100
```

This is a format example, not a personal schedule or a recommended calorie shift.
Choose a step from 0 to 300 kcal in multiples of four. The preview lists each date,
day type, calories, carbohydrate grams and calorie difference, plus fixed daily
protein/fat and the unchanged weekly total. The step is a planning preference, not
an estimate of calories burned.

Confirm the exact displayed A-number using `/allocation apply A<number>`. A new
draft for the same week invalidates older unapproved drafts. Applied allocations
remain active until a replacement is explicitly approved or the active allocation
is cancelled before its week starts. Repeating an approval cannot apply it twice.
Editing a Telegram message does not apply an allocation. Creating a proposal for
another week never reuses an earlier approval.

The deterministic rule assigns weights rest=0, gym/BJJ=1 and double=2, centers the
weights over explicitly known days, and multiplies by the chosen step. Unknown days
stay unknown and receive zero change. Four-kcal units use largest-remainder rounding
with chronological tie breaks, so the weekly calorie change is exactly zero and
carbohydrate changes remain whole grams. Protein and fat are unchanged. Invalid
negative-carbohydrate or out-of-range targets are rejected rather than clamped.

The explicit dates do not create completed sessions or modify recurring schedules.
Recurring schedule integration and food/timing suggestions remain separate work.
`/today`, weekly reporting and adaptive-review intake comparisons read the allocated
daily targets. The daily target view identifies the approved allocation.

Once the week starts, the allocation is fixed; midweek redistribution is not yet
supported. This preserves historical targets and the approved weekly budget. A
baseline goal or adaptive target change cannot overlap a current/future approved
allocation. Cancel a future allocation first, or wait for a started week to finish.
If the baseline changes after an unapproved preview, approval is rejected and a
fresh preview is required. Earlier proposals and resolutions remain immutable.
