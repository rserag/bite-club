# Training logs

The deployed T06 training ledger provides fast, auditable gym and BJJ session logs, optional detailed gym sets, optional reported BJJ stages, private usual-session templates, explicit training plans, and optional recovery check-ins. Local source also provides deterministic workload reports and conservative guidance through `/load`; training-day nutrition remains the next T06 slice. The deployed release predates `/load`; full real-device acceptance of the newer deployed flows is still pending.

Quick logs accept conversational forms:

```text
gym chest and triceps for 40 mins
gym legs 60 min rpe 7
BJJ 75 min medium intensity
BJJ 60 min hard rpe 8
```

Gym requires a reported focus and duration. BJJ requires duration and accepts an optional easy/medium/hard label. `rpe` is whole-session effort on a 0–10 scale and is distinct from a future lifting-set RPE. Duration is limited to 1–600 minutes and RPE to one decimal place.

If RPE is omitted, the receipt labels its source. A gym session uses the median only after at least three current, non-deleted, comparable sessions with reported RPE; otherwise it uses the visible default of 6. BJJ easy/medium/hard maps visibly to 3/5/8; an unlabeled BJJ session uses the visible default of 5. An inferred value contributes to approximate `duration × session RPE` load but never becomes fresh reported evidence for later estimates.

The quick path stores no exercises, sets, repetitions, lifted weights, BJJ rounds or stage durations. It does not estimate exercise calories and does not add calories back to a nutrition target. Add reported gym details later to the current receipt:

```text
/gym details T1r1 bench press: 10x60kg @8 warmup, 8x60kg @9 working; pull-ups: 8, 7
```

Exercise blocks use semicolons and sets use commas. A set may contain reps and external kilograms (`10x60kg`), reps only (`8` or `8 reps`), load only (`60kg`), a bodyweight convention (`10xbodyweight` or `bodyweight`), set RPE (`@8`), and an optional `warmup` or `working` flag. Missing fields remain unknown. Set RPE is displayed separately from whole-session RPE. `/gym details T1r2 clear` creates a new empty detail snapshot; it does not delete old revisions.

Reported BJJ detail is also optional and belongs to the same session:

```text
/bjj details T2r1 warmup=10 technical=20 positional=2x3 sparring=3x5
/bjj details T2r2 clear
```

Every stage is optional. `2x3` means two reported rounds of three minutes. Omitted stages remain unknown. The bot rejects a known stage sum longer than the reported whole-session duration. Stage time is descriptive within that total and is never added on top of it. A summary correction preserves the current detail snapshot; reducing the whole-session duration below its known stage sum requires correcting or clearing the detail first.

The private usual-session template records ranges as context, not completed facts:

```text
/bjj template set duration=60 warmup=8-12 technical=20-30 positional=1-2x3 sparring=2-4x5
/bjj template
/bjj template clear
```

Template ranges never populate actual rounds or stage durations. They do not classify the whole class as sparring and do not create attendance. Template history stays in the private database; the repository contains only synthetic examples.

Plans use a separate calendar ledger:

```text
/plan weekly bjj tue thu 60 min at 18:30
/plan 2026-10-01 bjj 60 min at 19:00
/plan 2026-10-01 cancel
/plan 2026-10-02 rest
/plan 2026-10-01 clear
/plan week
```

A date override wins over the recurring rule; `clear` reveals the recurring rule again. `cancelled`, `rest`, `planned`, `completed`, and `unknown` are kept distinct. A missing plan is unknown rather than rest. `/plan week` shows planned state and completed logs separately, including two scheduled activities on the same day. Neither a recurring rule nor a date plan can create a completed training session.

Each receipt has a `T…r…` revision reference and Edit/Delete/Undo buttons. Edit shows the explicit replacement command; the command replaces the quick summary on the same stable session:

```text
/gym edit T1r1 chest and triceps 50 min rpe 6.5
/bjj edit T2r1 75 min hard rpe 8
/training delete T1r2
/training undo T1r3
/training history
```

Old receipt buttons and old revision references cannot overwrite newer revisions. Delete removes the session from current history while retaining audit history; Undo appends another revision. The original Telegram message cannot be silently edited into a training correction.

`training_sessions` holds stable identity and the current pointer. `training_session_revisions` stores immutable kind/date/duration/focus/intensity/session-RPE/provenance snapshots. Gym and BJJ detail tables belong to one exact session revision, so later replacement, summary correction, deletion and undo cannot rewrite old data or create a second workout. Template revisions, schedule revisions and per-date plan events are separate immutable histories. Source-message, action and revision uniqueness make replay idempotent. Structured sessions remain local authoritative data; aggregate workload is calculated on demand rather than stored as a second source of truth.

## Workload aggregation

The local analytics layer behind `/load` and `/load short` calculates session-RPE workload as `duration minutes × session RPE`. It retains tenths of an arbitrary unit internally, reports gym, BJJ and total sums, and separates reported effort from values inferred through personal history, an intensity mapping or a visible default. Only the current non-deleted session revision contributes.

Calendar comparisons use the most recently completed Monday–Sunday week and the preceding four calendar weeks. A date is covered only by a current completed session or an explicit `rest` event. A plan is not attendance, a cancellation is not proof of rest, and a missing date remains unknown. The bot suppresses percentage comparisons when the latest week is incomplete, fewer than three reference weeks have all seven dates covered, or the median reference load is zero. This prevents sparse history and a new return to training from appearing as a precise workload spike.

A total increase of at least 30% prompts a recovery review only; it is explicitly not an injury-risk threshold. The report offers lighter-gym options only when BJJ load is also at least 30% above its eligible modality baseline and at least two check-in dates report soreness or fatigue at 4–5/5. The options are modest: remove one working set per exercise, avoid grinding sets, or move the session. Missing evidence is shown, and `/load` never changes a workout or plan.
