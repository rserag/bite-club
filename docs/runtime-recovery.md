# Bounded runtime recovery

The inbox permits at most 1,000 pending updates. If a received batch would exceed
that limit, its transaction rolls back, including the cursor. The receiver pauses
and retries from the committed offset. The processor reserves room for both possible
callback replies before changing any application record; queued/sending replies are
bounded at 1,000. Failed replies keep the existing bounded retry and retention rules.
Backpressure can delay service during a long Telegram outage; it never acknowledges
an unsaved batch or turns failed replies into successfully delivered messages.

## Operator host stall checker

The checker is optional host tooling. It needs Docker access on the host; the bot
container still has no Docker socket or host credentials. Start in observation mode:

```sh
python -m nutrition_bot.runtime.watchdog \
  --container synthetic-bot \
  --state-file /srv/bite-club/ops/watchdog.json \
  --deployment-lock /srv/bite-club/ops/deployment
```

Replace the example container with the exact service container. Keep the state and
deployment-lock parent directory private. The checker only inspects that container.
It needs at least three consecutive stale-heartbeat observations spanning three
minutes, with no observation gap above two minutes. A container generation change,
healthy result, intentional stop, or clock discontinuity resets observation.

Add `--restart` only when deliberately activating recovery, for example through a
host timer running once per minute. Even then, one restart attempt at most is allowed
per fifteen minutes. Cooldown is saved before invoking Docker, so a failed or
interrupted restart cannot immediately loop. Docker state is rechecked before the
request. Corrupt state or failed inspection fails closed without restarting.

Use the same deployment lock for all manual stop/migrate/start operations. The
checker uses an advisory flock on the supplied path plus `.lock`. On a Linux host:

```sh
flock -n /srv/bite-club/ops/deployment.lock sh /path/to/operator-deployment-script
```

Do not schedule the checker until existing deployment procedures honor that lock.
Its own state lock prevents overlapping checks. Retain the state across invocations.
No timer is installed or enabled automatically by this repository.

Fresh heartbeats during external API backoff remain healthy with degraded status.
They do not justify a restart. A database with less than 64 MiB available space is
reported as `disk_low`; missing/unreadable databases and schema mismatches also
require operator attention. The checker reports `attention_*` for these conditions
and never attempts to repair them through restarts. Connect these statuses and
nonzero command exits to the operator's existing alerting route.

## Migrations and recovery evidence

`migrate` and `run` retain their shared exclusive database lock. Take and verify a
snapshot with the old release before upgrading, as described in the manual deployment
runbook; run migration while stopped. A failed migration may leave schema work that
requires restoring the matching snapshot. Preserve the failed database and its newer
records before rollback. Never assume that an older image can use a newer schema.

Offline tests migrate both recovery-checkin and supplement-foundation schemas to
head, preserve synthetic records, verify retained snapshots, repeat migration, and
inject real `SQLITE_FULL` through a temporary database page limit. A failed action
leaves no partial profile/action/outbox state and can be replayed once after recovery.

Docker handles exited processes and boot recovery. This optional checker handles
stalled running processes. Neither can notify during total host failure: use an
independent external uptime/dead-man service if required, configured privately with
an operator-chosen endpoint. No external service is activated by this tooling.
