# Foundation operations and verification

This documents the implemented runtime and its failure boundaries. The [manual deployment runbook](manual-deployment.md) gives operator procedures. Tests use synthetic fixtures; host inventory, production logs and actual deployment records are private. AI remains disabled.

## What is durable

The receiver commits allowed updates and its next polling cursor in one database transaction. Unauthorized updates advance the cursor without storing their content. The processor rechecks authorization, then commits the application action and its outgoing response together. Application failures roll back the whole transaction; a new process can resume the still-pending input. Malformed persisted payloads are marked failed and stripped of content so they do not block the next update.

Inbox update IDs and action keys are unique. Callback actions also require a token issued by this bot, the matching delivered message ID/private chat, and a non-expired button. `/start` initializes one profile; repeated starts preserve its existing timezone. Food, goal, training, supplement and reporting actions share this authorization and transaction boundary.

Replies use an outbox. A process interrupted during delivery requeues the unfinished send at restart. Telegram may have accepted a message just before the process died, so a reply can appear twice; its application action is still applied once. Exactly-once network delivery is not promised.

Telegram retains undelivered updates for a limited period (documented as up to 24 hours), so the local inbox cannot recover content that never reached the VM during a longer outage. [Telegram getUpdates](https://core.telegram.org/bots/api#getupdates)

## Failure and health behavior

Polling/network failures use bounded backoff and honor Telegram's Retry-After. Outgoing transient errors persist retry times; after eight failed attempts the reply is retained as failed. `/status` shows failed reply count. Fix the underlying issue before requeuing. Authorization failures, a conflicting receiver, or an existing webhook require operator attention; the bot does not silently delete a webhook or take over another deployment.

To retry failed, unexpired message replies, stop the bot, then run:

```sh
uv run nutrition-bot retry-replies
```

Or with Compose:

```sh
docker compose stop bot
docker compose run --rm bot retry-replies
docker compose up -d
```

This only requeues messages for the currently configured private chat. It does not replay application actions, retry expired callback answers, or recover scrubbed content. Ambiguous prior delivery can still produce a duplicate reply.

Health checks require a matching schema and recent progress from the receiver, processor, sender, and cleanup loops. An API outage is reported as degraded while healthy local loops remain alive. A fatal loop error terminates the process; `restart: unless-stopped` can recover crashes after restart/reboot when Docker itself is enabled. Docker does not restart a merely unhealthy but still-running container by default; host-level detection/recovery remains a later operations task.

Health/schema checks are local and do not depend on Telegram or AI availability. SIGTERM/SIGINT stops the loops and marks their heartbeats stopped. A file lock prevents two local workers or a migration from owning the same database simultaneously. Different databases cannot share one Telegram bot token safely; Telegram polling-conflict errors expose that operator mistake.

## Retention and sensitive files

Completed inbox payloads and sent/failed reply content are scrubbed after 30 days by the cleanup loop. Minimal action/update IDs remain for duplicate protection. Photo files are not downloaded in this increment. Persistent meal drafts expire after seven inactive days; retained source/action associations support cleanup without erasing unrelated accepted records.

The data directory, populated environment files, private onboarding notes, logs, exports, and backups are excluded from Git/build context. The container copies only package/build inputs. Source distributions use an explicit file allowlist and wheels include migrations. Logs emit event names/error classes, not exception messages, tokens, user input, or provider payloads.

Operator-run consistent snapshots, encrypted restic upload/retention, and isolated restore verification are available in the [backup guide](backup-and-restore.md). Automatic scheduling and verified off-site recovery remain incomplete. The explicit `migrate` command applies the current schema; production rollback and off-site restore drills belong to T11/T12.

## Verification boundaries

The offline suite exercises authorization, duplicate/replayed updates, transaction rollback, hard process death, ambiguous delivery, persistent retry scheduling, migrations, file locks, retention and health. Feature suites add the ledger, approval, recipe, goal, training, supplement and nutrient-report contracts.

The separate real-user Telegram runner verifies selected scenarios through a dedicated test bot, including measured meals, stale approvals, correction/delete/undo, restart, supplement reporting, adherence and incomplete nutrient coverage. Normal CI is offline; live credentials and reports are never CI inputs or artifacts. See [Telegram testing](telegram-e2e.md).

A deterministic connection-opening cancellation test reproduces and guards a SQLite handle leak. The runtime waits for opening and cleanup before propagating cancellation, including repeated cancellation. This addresses the opening race described in [aiosqlite issue 259](https://github.com/omnilib/aiosqlite/issues/259).

Container smoke checks verify non-root execution, packaged/repeated migrations and persistence across fresh containers without network access. They do not prove complete production recovery, host boot behavior, off-site backups or automatic failover. Exhaustive tests belong on a development/CI runner, not a capacity-constrained runtime host. Before operating with important data, independently verify backup and restoration for that deployment.
