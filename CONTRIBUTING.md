# Contributing to Bite Club

Bring a reproducible case, preferably a failing test. The most useful contributions make an uncertainty explicit, protect a ledger invariant, or turn a real workflow into an executable acceptance scenario.

## Development

Use Python 3.13 and uv:

```sh
uv sync --locked --group e2e
uv run python scripts/demo.py
uv run --group e2e ruff check .
uv run --group e2e ruff format --check .
uv run --group e2e mypy
uv run --group e2e pytest tests tools/telegram_e2e/tests -q
```

Normal tests use local synthetic data and require no external accounts. Live Telegram tests are opt-in; use a dedicated bot/account and the [runner guide](docs/telegram-e2e.md). Never connect the runner to a personal diary database or a production bot token.

## Contracts to preserve

- Unknown nutrients stay unknown. Numeric zero must come from evidence.
- A rough quantity stays a draft until the exact displayed revision is approved.
- Current reports follow current revisions; historical source snapshots remain immutable.
- Replayed updates and old buttons cannot duplicate or overwrite accepted actions.
- Plans are not attendance, and scheduled doses are not consumed doses.
- External calls stay outside write transactions. Failures do not fabricate successful saves.

Use migrations for schema changes. Add tests at the level that demonstrates the behavior, then run the relevant suite, Ruff and mypy. Container changes also require the existing smoke check. Do not add superficial tests merely to increase the count.

## Pull requests

Describe the problem, resulting behavior and evidence. Identify changes to approval, provenance, privacy or data migration. Separate implemented behavior from proposals and unverified claims. Use synthetic fixtures and transcripts only; do not attach personal food records, Telegram sessions, tokens, raw logs, database copies or deployment inventory.

Public work can be discussed in GitHub issues. Maintainer-local Beads/Dolt records are optional development tooling and are not shipped or required to contribute. Planned scope lives in the design documents; a plan is not evidence that a feature exists.

By contributing, you agree that your contributions are licensed under the project's MIT license. Keep third-party attribution and data terms intact.
