# Settings and optional reminders

Use `/settings` to see the saved timezone, report length, quiet hours and each reminder category. `/settings setup` shows editable options. Every category starts **off**; no personal schedule is included in public defaults. Choosing a time with `on` explicitly enables that category.

The examples below are synthetic:

| Change | Command |
| --- | --- |
| Timezone | `/settings timezone Europe/Paris` |
| Report length | `/settings report short` or `/settings report full` |
| Weight check-in | `/settings weight 08:00 on` |
| Recovery check-in | `/settings recovery 08:05 on` |
| Evening summary | `/settings evening 20:30 on` |
| Weekly review | `/settings weekly Monday 09:00 on` |
| Preparation prompt | `/settings training_pre on 120` |
| Logging prompt | `/settings training_post on 15` |
| Overnight quiet hours | `/settings quiet 23:00 07:00` |
| Disable quiet hours | `/settings quiet off` |
| Disable one category | `/settings weight off` |
| Disable every category | `/settings reminders off` |

Use an IANA timezone and unambiguous 24-hour clock times. Equivalent explicit phrases include “set timezone Europe/Paris”, “remind me to weigh at 08:00”, “weekly review Monday at 09:00”, and “disable recovery reminders”. Each change produces a saved-settings receipt. The receipt shows the next clock-based notifications; quiet hours can block a selected time.

Daily and weekly reports start compact. The saved report-length setting applies to `/today`, `/week`, their supported natural phrases, and scheduled summaries. Explicit `/today full`, `/today short`, `/week full` and `/week short` override the setting for that view only; dates can be included before or after the view. **Details** and **Short version** buttons keep the original report date, read current diary revisions, and never mark food complete. A temporary expanded view does not change your saved preference.

Preparation reminders use minutes **before a known planned start**. Logging reminders use minutes **after the approximate planned end**. Configure training through `/plan weekly bjj tue thu 60 min at 18:30` or a date override such as `/plan 2026-10-08 bjj 60 min at 18:30`. A weekday-only plan without a start time does not schedule relative prompts. `/plan 2026-10-08 cancel` suppresses that date’s prompts. Planned sessions and reminder delivery never record attendance, actual duration, meals, body weight or recovery metrics.

Weight and recovery reminders are suppressed when a current, non-deleted check-in already exists for the reminder’s local date. Completed training suppresses that kind’s planned prompts. Post-training prompts omit a recovery question when recovery is already recorded. The sender checks these conditions again immediately before issuing a Telegram request. Evening summaries read current recorded intake and reviewed targets, preserving unknown nutrition and completeness. Weekly reviews cover the **preceding completed Monday–Sunday local week**, regardless of the chosen review weekday. These summaries require no AI calls.

Preferences live in the private database and survive restarts. Environment timezone values seed new profiles; they do not overwrite Telegram settings. Timezone edits retain chosen local clock times, invalidate unsent payloads, and update future notifications while preserving existing log dates, UTC timestamps, correction history and the AI budget ledger. The user can inspect the immutable saved-settings history in the database’s private `settings_events` records.

Each job has a stable category/date identity, plus training kind for session-relative prompts. Sending a notification does not create a new identity when the timezone or plan changes. During an autumn DST fold the earliest occurrence is used once. A spring DST gap advances to the first valid local minute. Quiet hours suppress prompts rather than postponing them to an unrequested time.

After downtime the scheduler discards obsolete prompts. Freshness windows are one hour for weight, recovery and post-training prompts; thirty minutes for preparation prompts, always before the planned start; two hours for evening summaries; and six hours for weekly reviews. A category enabled after today’s chosen time waits for the next eligible occurrence. A bounded scheduler queues at most twenty due jobs per tick and observes the existing outbox capacity. Retries use the existing bounded sender policy and are checked against expiry again. Telegram may repeat a message if the process dies after the request succeeds but before its acknowledgement is committed; application records and logical job identities remain idempotent.

Runtime integration uses `scheduler_tick(store, settings, now=...)` and `guard_scheduled_reply(connection, reply, settings, now=...)`. All scheduling, suppression and report rendering are local transactions; Telegram calls happen afterward through the existing outbox. Synthetic inbox identities occupy a reserved negative range distinct from Telegram updates and the Mini App, and never advance the Telegram polling cursor. Notifications remain bound to the configured allowed owner and private chat.
