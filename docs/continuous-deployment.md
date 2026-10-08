# Continuous deployment

Merges to `main` run offline application tests, Ruff, mypy, publication checks,
container persistence/migration checks, Alpine compatibility tests, and secret,
dependency and image audits. The release workflow builds an immutable amd64 GHCR
image, attaches SBOM/build provenance, smoke-tests and scans that exact digest,
then enters the protected `production` environment. An operator can also dispatch
the release workflow on `main` with its confirmation input enabled.

The production environment is restricted to `main` and requires owner approval,
matching the other applications on the deployment host. Configure `DEPLOY_HOST`,
`DEPLOY_PORT` and `DEPLOY_USER` as environment variables. Store only a dedicated
deployment SSH private key and the independently trusted SSH host-key entry as
`DEPLOY_SSH_PRIVATE_KEY` and `DEPLOY_KNOWN_HOSTS`. Application credentials,
diary records, backups and operator inventory remain outside GitHub.

## Host installation

An operator installs root-owned `deploy/compose.yml` and `deploy/dbcheck.py`
under `/opt/bite-club`, and the two executable controllers under
`/usr/local/sbin`. Provision `/opt/bite-club/runtime.env` as a root-owned regular
file with mode 0600. The Compose project remains `nutrition-bot`, using the
external `nutrition-bot_bot-data` volume. Do not run a second polling worker.

The optional Mini App binds container port 8080 to host loopback port 8010.
Configure its HTTPS hostname and Nginx using
[`nginx.conf.example`](../deploy/nginx.conf.example); authenticate each API
request with fresh Telegram init data. Create the separate credentials directory
`/opt/bite-club/credentials` with runtime UID/GID 10001 and mode 0700 before
installing Compose. Its writable `/auth` mount supports private OAuth token
rotation; credential files must remain mode 0600. Neither ingress nor AI is
enabled by the public environment defaults.

The production stop grace is 60 seconds so a rotating OAuth refresh can finish
its bounded identity verification and atomic token save before process exit.

Create a dedicated `bite-club-deploy` account without Docker group membership.
Its authorized key must use `restrict` and the forced command
`/usr/local/sbin/bite-club-deploy-ssh`. A root-owned, validated sudoers entry
allows only `/usr/local/sbin/bite-club-deploy --request *`. The controller accepts
only `release <40-character revision> <allowlisted image digest>`, `rollback`
and `status`; it does not evaluate request text as shell code. Application
credentials never become workflow inputs.

Host controllers and Compose are deliberately not overwritten by releases.
Changes to them require separate operator installation after review. This keeps
the restricted deployment key from granting general root code execution.

## Upgrade and recovery

The controller serializes operations, validates image provenance labels, pulls
before stopping the worker, backs up the entire stopped data volume, verifies
the archive checksum, and restores it into an isolated disposable volume.
Migration rehearsal has no network and checks SQLite integrity, foreign keys
and preservation of every pre-existing table's row counts and content hashes.
An intentional migration that rewrites history requires an operator-reviewed
procedure rather than bypassing this check. Production migration runs only with
the sole worker stopped. Backup/migration containers have bounded CPU and RAM.

A failed rehearsal restarts the unchanged previous release. A failed production
migration restores the pre-upgrade snapshot before restarting the old image.
If the candidate starts but fails health verification, it is stopped and a
second snapshot captures its state. The controller then requires operator
recovery: automatically restoring the earlier database could discard records
or Telegram cursor changes accepted by the candidate.

The protected rollback workflow selects the previous recorded image and repeats
the same backup and rehearsal gates. It cannot downgrade an incompatible schema.
For schema-changing recovery, inspect both snapshots and preserve any new
records before choosing a matching database/image pair. See the
[manual recovery procedure](manual-deployment.md). Never delete the live volume
or use `docker compose down -v`.

Successful releases record the exact image/revision under root-only `state/`
and `releases/`; cutover snapshots remain under root-only `backups/`. These local
archives cover rollout recovery, not host loss. Scheduled encrypted off-site
backups and deletion reconciliation remain separate implementation work.
