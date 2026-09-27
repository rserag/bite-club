# Manual deployment on one VM

This runbook targets a single operator-managed Linux VM. Automated delivery and host migration are separate tasks. This procedure uses SSH and Docker Compose; it does not require a registry, domain, inbound application port, or AI account.

The current manual release includes the implemented local diary features documented in the repository. AI, reminders, exports and automated backups remain unimplemented. Unsupported messages do not create nutrition records. Passing the checks below does not make the completed assistant production-ready.

The commands are examples to execute deliberately, not evidence that each drill has been performed. Use Bash on the VM. Replace the SSH alias and release identifier with your own values; keep host addresses and personal settings in private deployment notes.

## Layout and prerequisites

```text
/opt/nutrition-bot/
  releases/<release-id>/       immutable extracted source
  releases/<release-id>.tar.gz source package
  current -> releases/<release-id>
  shared/runtime.env          credentials and private runtime settings, mode 0600
  shared/deploy.env           image tag and runtime.env path, mode 0600
  shared/backups/             private manual backups, mode 0700
```

Use the fixed Compose project name `nutrition-bot`. Its default data volume is `nutrition-bot_bot-data`, mounted at `/data`. Changing release directories must not create a new database volume. Keep SQLite on a local filesystem.

Requirements:

- A reachable Linux VM with Docker Engine and Compose installed; Docker must be enabled at boot. Check available RAM and disk before building. This Compose service has a 512 MiB memory limit; the Docker builder is not covered by that limit. Build on a larger machine for the target architecture and transfer the image if the VM cannot build comfortably.
- SSH authentication and a verified host key. Configure a local SSH alias such as `nutrition-vm`; do not bypass host-key verification. Docker access is effectively privileged, so limit access to the deployment account.
- Outbound HTTPS to Telegram; image/dependency downloads during builds. No application port needs opening.
- A dedicated bot token, the allowed Telegram user ID, and that user's private-chat ID before starting polling. The bot must not be polling elsewhere or have a webhook configured.

Create the application directories on the VM, owned by the deployment account. Leave other applications and host networking unchanged:

```bash
sudo install -d -m 0750 -o "$USER" -g "$(id -gn)" \
  /opt/nutrition-bot /opt/nutrition-bot/releases /opt/nutrition-bot/shared
```

## Package and transfer a release

On the development machine, run the local checks before packaging:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
python3 scripts/package-release.py /tmp/nutrition-bot.tar.gz
```

The packager prints a content-derived release ID and file count. Its explicit allowlist includes application/build inputs, tests, and scripts, plus a SHA-256 manifest. It excludes private settings, Git metadata, databases, logs, local environments, and documentation other than the README. Do not replace it with an archive of the entire workspace.

Set `RELEASE_ID` to the printed ID, then transfer the source package:

```bash
RELEASE_ID='<release-id>'
scp /tmp/nutrition-bot.tar.gz \
  "nutrition-vm:/opt/nutrition-bot/releases/$RELEASE_ID.tar.gz"
```

On the VM, use a new release directory. Do not overwrite an existing release:

```bash
set -euo pipefail
BOT_ROOT=/opt/nutrition-bot
RELEASE_ID='<release-id>'
BOT_IMAGE="nutrition-bot:manual-$RELEASE_ID"
mkdir "$BOT_ROOT/releases/$RELEASE_ID"
tar -xzf "$BOT_ROOT/releases/$RELEASE_ID.tar.gz" \
  -C "$BOT_ROOT/releases/$RELEASE_ID"
cd "$BOT_ROOT/releases/$RELEASE_ID"
python3 - "$RELEASE_ID" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest = json.loads(Path("release-manifest.json").read_text())
assert manifest["release"] == sys.argv[1]
for name, expected in manifest["files"].items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name
release = hashlib.sha256(
    json.dumps(manifest["files"], sort_keys=True).encode()
).hexdigest()[:12]
assert release == manifest["release"]
print("Release file hashes verified.")
PY
```

The manifest detects a mismatched/corrupted transfer; it is not a signature. The trusted source checkout and authenticated SSH connection establish where the package came from.

## Transfer private configuration separately

On the development machine, create an ignored private file from `.env.example`. Set a valid `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_ID`, `ALLOWED_TELEGRAM_CHAT_ID`, and intended IANA `APP_TIMEZONE`. Both IDs must be positive; this release accepts only the configured user's private chat. Compose sets the container database path itself.

No paid AI key is used. A USDA key is optional for explicit remote food lookup; manual/local food workflows remain available. Timezone seeds the profile on its first `/start`; changing the environment later does not overwrite an existing profile's timezone. The settings UI is a later feature.

```bash
mkdir -p private
chmod 700 private
test -e private/deployment.env || cp .env.example private/deployment.env
chmod 600 private/deployment.env
# Edit private/deployment.env locally, then transfer it without printing its contents.
scp private/deployment.env nutrition-vm:/opt/nutrition-bot/shared/runtime.env.upload
```

On the VM, stage the uploaded file after setting its permissions:

```bash
chmod 600 "$BOT_ROOT/shared/runtime.env.upload"
mv "$BOT_ROOT/shared/runtime.env.upload" "$BOT_ROOT/shared/runtime.env.next"
umask 077
printf 'BOT_IMAGE=%s\nBOT_ENV_FILE=%s/shared/runtime.env\n' \
  "$BOT_IMAGE" "$BOT_ROOT" > "$BOT_ROOT/shared/deploy.env.next"
chmod 600 "$BOT_ROOT/shared/deploy.env.next"
```

Keep both new configuration files staged until cutover. Never put a token in a shell argument, screenshot, issue, source archive, or chat. Do not print `docker compose config`, container environment variables, or full container inspection output: they can expose secrets. `config --quiet` is safe for checking Compose syntax.

## Build and test before cutover

On the VM, build and test sequentially to limit peak memory:

```bash
docker build --tag "$BOT_IMAGE" "$BOT_ROOT/releases/$RELEASE_ID"
bash "$BOT_ROOT/releases/$RELEASE_ID/scripts/container-smoke.sh" "$BOT_IMAGE"
docker build --target test --tag "nutrition-bot:test-$RELEASE_ID" \
  "$BOT_ROOT/releases/$RELEASE_ID"
docker run --rm --network none --read-only --memory 512m \
  --tmpfs /tmp:size=256m "nutrition-bot:test-$RELEASE_ID"
```

The smoke script uses a disposable volume and no network. It verifies packaged migrations, non-root execution, and persistence across fresh containers. The test image runs the automated suite; it is not the deployed service image. Retain the current and previous production images for rollback. Avoid broad Docker cleanup commands that could affect other workloads or retained backups.

## Cut over, migrate, and start

For repeat operations, define this helper in the VM shell. The optional volume override is used only for an explicit recovery described below:

```bash
BOT_ROOT=/opt/nutrition-bot
bot() {
  local files=(-f "$BOT_ROOT/current/docker-compose.yml")
  if [[ -f "$BOT_ROOT/shared/volume-override.yml" ]]; then
    files+=(-f "$BOT_ROOT/shared/volume-override.yml")
  fi
  docker compose --project-name nutrition-bot \
    --env-file "$BOT_ROOT/shared/deploy.env" "${files[@]}" "$@"
}
```

For an upgrade, stop the old worker with `bot stop bot`, take the stopped-volume backup below, and record the old release/image before switching. On the first installation there is no old worker or database to back up. Never run migration alongside a worker; the local database lock rejects that conflict.

```bash
ln -s "releases/$RELEASE_ID" "$BOT_ROOT/current.next"
mv -Tf "$BOT_ROOT/current.next" "$BOT_ROOT/current"
mv "$BOT_ROOT/shared/runtime.env.next" "$BOT_ROOT/shared/runtime.env"
mv "$BOT_ROOT/shared/deploy.env.next" "$BOT_ROOT/shared/deploy.env"
bot config --quiet
bot run --rm --no-deps bot migrate
```

Migrations do not require Telegram credentials. If credentials are still missing, stop here: source, image, and database are staged, but no worker is running. A health check correctly fails while the worker is stopped.

With complete credentials and only one deployment using the token:

```bash
bot up -d --no-build
bot ps
bot exec -T bot python -m nutrition_bot healthcheck
bot logs --tail 100 bot
```

Allow startup and the first health-check interval before judging the result. The worker refuses an existing webhook and does not delete it. Resolve an unexpected webhook, invalid credentials, or polling conflict before restarting; do not repeatedly launch competing workers. Verify live behavior from the authorized user's private Telegram chat: send `/start`, send `/status`, and tap refresh. Then restart the service and verify it becomes healthy and retains its profile:

```bash
bot restart bot
bot exec -T bot python -m nutrition_bot healthcheck
```

The local health check distinguishes loop health from a degraded Telegram connection. Docker restarts a crashed process through `unless-stopped`; it does not automatically restart a merely unhealthy container. An explicitly stopped service stays stopped across reboot until started again. Check `systemctl is-enabled docker` when validating boot behavior.

Logs contain event names and error classes, with rotation configured by Compose. Inspect selected status/health fields instead of dumping environment/configuration. See [foundation operations](foundation-operations.md) for outbox retry behavior and limitations. To retry failed, unexpired replies after fixing the cause, use `bot stop bot`, `bot run --rm bot retry-replies`, then `bot up -d --no-build`.

## Manual backup and isolated restore drill

Automated backups and off-site delivery are not implemented. The following is an operator-run procedure, not a claim that backup or recovery has already been tested. A backup on the same VM does not cover VM loss. Copy verified backups to separate private storage, using encryption before storing them with an external service. Keep at least one previous verified snapshot.

Stop the only worker and keep it stopped throughout the archive. Use the actual current production image in `BOT_IMAGE`. If a recovery override is active, use its volume name instead of the default below. All writers must be stopped, including one-off maintenance containers. Do not copy only the SQLite main file while it is live: committed records may still be in its WAL.

```bash
bot stop bot
umask 077
install -d -m 0700 "$BOT_ROOT/shared/backups"
BACKUP_FILE="$BOT_ROOT/shared/backups/data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
DATA_VOLUME=nutrition-bot_bot-data
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,src=$DATA_VOLUME,dst=/data,readonly" \
  --entrypoint python "$BOT_IMAGE" -c '
import sys
import tarfile
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
    archive.add("/data", arcname=".")
' > "$BACKUP_FILE"
sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
bot up -d --no-build
```

If the archive command fails, treat the output as incomplete and resolve that failure before relying on it. Store the corresponding release package/manifest and image identifier with the backup. Back up `runtime.env`, `deploy.env`, and any volume override separately under the same private/encrypted storage policy; credentials are necessary for recovery but do not belong in a public source archive. Preserve the files' restrictive permissions.

Test recovery using a new, empty volume and no network. Never point this drill at the running data volume:

```bash
RESTORE_VOLUME="nutrition-bot-restore-$(date -u +%Y%m%dT%H%M%SZ)"
sha256sum -c "$BACKUP_FILE.sha256"
docker volume create "$RESTORE_VOLUME"
docker run --rm -i --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,src=$RESTORE_VOLUME,dst=/data" \
  --entrypoint python "$BOT_IMAGE" -c '
import sys
import tarfile
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    archive.extractall("/data", filter="data")
' < "$BACKUP_FILE"
docker run --rm --network none --read-only \
  --mount "type=volume,src=$RESTORE_VOLUME,dst=/data" \
  --entrypoint python "$BOT_IMAGE" -c '
import sqlite3
with sqlite3.connect("file:/data/app.sqlite3?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT version_num FROM alembic_version").fetchone()
print("Restored database integrity verified.")
'
docker run --rm --network none --read-only \
  --mount "type=volume,src=$RESTORE_VOLUME,dst=/data" \
  "$BOT_IMAGE" migrate
```

Record the snapshot time, image/release, successful integrity/migration checks, and elapsed restore time in private operations notes. If the volume was only a drill, remove that exact test volume after checking its name. Do not start its worker: that would compete with the live bot.

For actual recovery, stop the live worker and preserve its existing volume. Point Compose at the verified restored volume using a private `shared/volume-override.yml`:

```yaml
volumes:
  bot-data:
    external: true
    name: REPLACE_WITH_VERIFIED_RESTORE_VOLUME
```

Set the exact verified volume name, restrict the file to mode 0600, and use the `bot` helper for every subsequent operation so this override remains active. Select the image compatible with that backup, validate with `bot config --quiet`, migrate while stopped if necessary, then start and verify health/live Telegram. Retain the old volume until recovery is accepted. Restoring an older snapshot loses subsequent local records and may cause Telegram updates still available upstream to be replayed; Telegram does not retain an unlimited history.

## Rollback and later migration

For an application-only rollback, stop the worker, take a backup, restore the previous `current` symlink and its exact `BOT_IMAGE` in `deploy.env`, then start with `--no-build`. This works only if the previous application supports the current schema and stored data. Do not assume migrations can be downgraded. When compatibility is absent, restore a matching verified database snapshot and image using the recovery procedure; account for records created since that snapshot.

For migration to another host, prepare and test the target image for its architecture first. Stop polling on both hosts, archive the source volume while stopped, transfer the verified backup and private configuration separately, and restore/validate on the target. Start only the target worker, check Telegram behavior and health, and leave the source stopped as a rollback candidate. Do not restart the source against an older database after accepting new records on the target without a plan to preserve those records.

The GitHub transition should publish reviewed source and synthetic fixtures only. Keep runtime databases, tokens, personal settings, deployment inventory, backups, logs, and exports private. Retain the same Compose project/volume identity through future release automation. Never use `docker compose down -v` for an upgrade, rollback, or host migration.
