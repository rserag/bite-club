# Backup and isolated restore

The operator can create consistent local snapshots, upload them to an encrypted
restic repository, and verify an isolated restore. These commands do not schedule
backups, provision storage, modify production data, or start a Telegram worker.
Off-site destination setup and a real recovery drill remain explicit operations.

## Local snapshot

Use the application version matching the database schema. Create a private parent
directory outside the source checkout and supply a new output directory:

```sh
install -d -m 0700 /srv/bite-club/backups
nutrition-bot backup --output /srv/bite-club/backups/snapshot-001 \
  --image-digest 'sha256:<64-hex-image-digest>'
nutrition-bot restore-check --input /srv/bite-club/backups/snapshot-001
```

Replace the digest placeholder with the immutable image digest actually running.
The database path comes from `DATABASE_URL`; no Telegram token is needed. SQLite's
online backup API includes committed WAL data without stopping the worker. A busy
snapshot has a 60-second copy deadline. Integrity, foreign keys, schema revision,
table counts, and every file checksum are verified. Output permissions are private.
A failed snapshot is removed; an existing output directory is never overwritten.

The current application has no photo storage. When using a separate retained-media
directory, stop the worker and pass `--media-root /path/to/media`. The command
acquires the same exclusive database lock as the worker and copies that entire
explicit media root. Keep exports, credentials and unrelated files out of it.
Symlinks and special files are rejected. Future photo retention must use this lock
or supply a snapshot-aware media protocol before live media snapshots are supported.

The manifest contains schema revision, image digest, UTC timestamp, file checksums
and table counts. It is private operational data. A local snapshot is plaintext and
does not protect against loss of the host. Do not upload it directly to public storage.

## Encrypted repository

Install restic on the operator host, outside the bot image. Configure a dedicated
repository with credentials available only to the backup operator. Initialize it
deliberately using restic; these commands never initialize or purchase storage.
Use an independently stored password file with mode 0600. Never commit its contents.

```sh
export RESTIC_REPOSITORY=/path/to/private/encrypted-repository
export RESTIC_PASSWORD_FILE=/path/to/private/password-file
python -m nutrition_bot.runtime.backup_repository upload \
  --input /srv/bite-club/backups/snapshot-001
```

Use an off-host backend for actual disaster recovery. A successful upload returns
an exact snapshot ID, distinct from local snapshot success. Any partial/error exit
fails closed. Backend output is suppressed because it may include private paths.
Each restic invocation has a ten-minute deadline. Credentials never enter manifests
or command-line arguments. Protect the local restic cache as private state too.

Restore that exact snapshot to a new isolated directory:

```sh
python -m nutrition_bot.runtime.backup_repository restore \
  --snapshot '<full-64-hex-snapshot-id>' \
  --output /srv/bite-club/restore-drill-001
```

Restic verifies restored bytes; the application then independently verifies the
manifest, SQLite integrity, foreign keys and counts. An existing destination is
rejected. Failed restores remain isolated for diagnosis and must never be used as
runtime data. Verification does not import or start Telegram and performs no sends.
It does not rewrite the restored database or automatically promote it to production.

## Retention

Preview seven daily, four weekly and six monthly snapshots:

```sh
python -m nutrition_bot.runtime.backup_repository retention
```

The preview reports snapshot IDs selected for removal. Only after reviewing it,
repeat with `--apply` to forget/prune. Selection is restricted to the dedicated
`bite-club-v1` host/tag and grouped by host/tag so timestamped staging paths do not
silently prevent rotation. Use a separate repository for each independently operated
diary. Never apply retention before a successful isolated restore. Plaintext staging
directories are not pruned by this command; remove specific local copies deliberately
after verifying recovery, keeping any needed pre-migration snapshot.

## Actual recovery

Keep the live worker stopped and preserve its original volume. Use the image digest
recorded in the manifest and the manual deployment runbook. A snapshot restores
pending inbox/outbox state as well as diary records: do not start a restored worker
until pending work has been reviewed and quarantined where needed. Records accepted
after the snapshot can be missing, and Telegram cannot reconstruct unlimited history.
Future permanent-erasure support must reconcile deletions before sends resume.

Synthetic local round-trip tests prove the tool path, not access to an off-site
provider or a production recovery time. Scheduled backups, independent host-failure
alerts, and a real off-site drill remain separate acceptance work.

References: [SQLite online backup](https://www.sqlite.org/backup.html),
[restic backup](https://restic.readthedocs.io/en/stable/040_backup.html),
[restore](https://restic.readthedocs.io/en/stable/050_restore.html), and
[retention](https://restic.readthedocs.io/en/stable/060_forget.html).
