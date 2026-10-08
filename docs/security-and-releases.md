# Security checks and image releases

The security workflow scans reachable public Git history with pinned Gitleaks and
redacted output, audits all locked runtime/development dependencies with pip-audit,
and scans the built runtime image with Trivy. It runs on pull requests, main pushes,
weekly schedules, and explicit dispatch. Actions are pinned to full commit SHAs;
scanner versions are explicit. Pull-request jobs use read-only permissions and no
production credentials. They never run live Telegram or paid model tests.

Known dependency vulnerabilities fail the Python audit. HIGH/CRITICAL container
vulnerabilities fail image checks, including those without a published fix. There
are currently no vulnerability suppressions. A future exception must identify the
advisory/package, exposure reasoning, compensating control, owner and expiry; it
must be reviewed in a PR and wired explicitly into the relevant scan. Do not mask
an advisory-feed outage or scanner error as a successful scan. Lower-severity image
findings remain a maintenance concern even when they do not block this gate.

Gitleaks has exact exceptions only for named synthetic Telegram fixtures. Never add
an entire directory to an allowlist to hide a real credential. Public Actions logs
and artifacts must not contain private databases, tracker exports or backend logs.

## Runtime base and compatibility

Build, test, and runtime stages use the same digest-pinned official Python 3.13
Alpine 3.24 image. This removes unused Debian system utilities implicated by the
previous image scan; vulnerability checks retain the same severity threshold and
continue to include unfixed advisories. The runtime retains its package metadata
for scanning and does not contain pip or development dependencies.

Alpine uses musl rather than glibc. Native Python dependencies must install and
pass the offline test suite in the Alpine test stage; a successful host Python
test run alone is insufficient. Container smoke checks also verify non-root
persistence, migrations, SQLite backup/restore, timezone data and TLS trust roots.
Revalidate these checks and the image scan when changing the base digest.

## Release eligibility

The release workflow runs on `main` pushes or explicit dispatch with
`confirm_publish` enabled. It reruns offline/container checks and security checks
before a job with package-write permission publishes an image. Only the subsequent
protected production job receives the restricted deployment SSH key. Bot credentials
remain on the host and are never available to build or pull-request jobs.

The current workflow builds `linux/amd64`; verify your destination architecture
before using it. ARM64 release validation must be added before using these images
on an ARM64 host. The commit-based tag is a convenience pointer. Only the resulting
`ghcr.io/<owner>/<repository>@sha256:<digest>` is an immutable deployment reference.
SBOM and build provenance attestations are attached to the image. The final scan
checks that exact published digest; if it fails, the uploaded candidate is **not
eligible for deployment**. Only a successful run records a verified image reference
in its summary. Rebuilding the same commit may produce a different digest.

Verify hosted checks, registry permissions and artifact attestations before
approving production deployment. Local workflow validation does not prove GHCR
publication or hosted release success. Security checks should remain required main
checks after their first successful hosted run.

## Deployment and rollback

Follow the manual deployment and backup runbooks. Acquire the shared host deployment
lock; record the running digest; stop intake; create and verify the pre-migration
snapshot; migrate using the selected new image; start exactly one worker; verify
local health and deliberately authorized Telegram acceptance. Do not start a second
poller for a smoke test.

Record the old and new digests, snapshot identifier, source revisions, schema
revisions, health result and restore evidence in private operational notes. Keep
the matching previous image and verified database snapshot. If the schema is not
backward-compatible, image-only rollback is unsafe. Preserve any newer records before
restoring old state. The protected workflows deploy and request schema-compatible rollback through a
restricted host controller. Host migration and scheduled off-site backup activation
remain separate operator work.

## Continuous deployment

The protected release pipeline and restricted host controller are documented in
[continuous-deployment.md](continuous-deployment.md). Merges to main now publish
verified immutable images and queue production deployment for owner approval.
