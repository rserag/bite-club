# Recovery check-ins

Recovery check-ins are an optional subjective diary. Log any subset of sleep, soreness, fatigue and readiness in one message:

```text
recovery sleep 7.5h
recovery soreness 2 fatigue 3 readiness 4
recovery sleep 8h fatigue 2
```

Sleep is stored as whole minutes from 0–24 hours. Soreness, fatigue and readiness are independent reported 1–5 values. Missing values remain unknown; the bot does not fill them from training history, combine them into a hidden score, diagnose overtraining, or make a medical claim.

There is one current check-in per local calendar date. A second check-in for that date is rejected with a pointer to edit the current receipt, preventing two partial records from being mistaken for one complete day. Edit replaces the snapshot, so omitted values become unknown:

```text
/recovery edit R1r1 sleep 7.5h fatigue 3
/recovery delete R1r2
/recovery undo R1r3
/recovery history
```

Receipts include Edit, Delete and Undo buttons. Old buttons and revision references cannot overwrite a newer correction. Delete and Undo append revisions; they do not rewrite history. The stable check-in row holds the local date and current pointer, while immutable revisions hold only reported values and their action provenance. Recovery data remains in the private database and is not included in public examples or repository configuration.
