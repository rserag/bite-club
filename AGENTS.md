# Working on Bite Club

Read the README for implemented scope. `docs/feature-decisions.md` records intended product scope, `docs/technical-proposal.md` records the design, and `docs/implementation-plan.md` gives acceptance criteria. Do not treat a planned feature as an implemented capability.

## Data and behavior contracts

- Keep calculation and state changes deterministic. Never invent nutrients or turn unknown data into zero.
- Uncertain portions need explicit approval of the exact displayed draft revision. Old approvals do not apply to edited drafts or repeated meals.
- Preserve immutable source snapshots and correction history. Current reports read current revisions.
- Keep external calls outside database write transactions. Preserve inbox/outbox idempotency, authorization and bounded failure behavior.
- Keep examples, fixtures, demos and evaluations synthetic. Never commit credentials, account identifiers, personal schedules, logs, photos, databases or backups.

## Development workflow

- Use targeted tests, Ruff and mypy for application changes. Container changes also require the container smoke check.
- Do not run live Telegram tests, paid model evaluations or deployments as ordinary unit tests or pull-request CI.
- If a local Beads workspace is present, use its installed skill and `bd` for maintainer task tracking. Its database, histories and exports remain private. Without it, use the public issue workflow; Beads is not needed to build or test the app.
- Keep public source, private deployment state and development tooling separate. Do not install tracker tooling in the runtime image.
- Source changes do not imply authorization to commit, push, publish, deploy, change remote settings or purchase services. Follow the user's explicit scope.
