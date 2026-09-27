# Running real-user Telegram tests

The development runner uses Telethon to authenticate as a user and interacts with a separate test bot through Telegram. The unmodified bot worker runs locally with a fresh migrated SQLite database per scenario. These tools are outside the production package; the production host and real diary bot are not involved.

## Private setup

A commented sample is at `tools/telegram_e2e/config.example.toml`. The initial private copy for this workspace is `private/telegram-e2e/config.toml`. Fill it in locally; do not send its contents in chat or put it in Git.

For a fresh checkout:

```sh
mkdir -p private/telegram-e2e
chmod 700 private/telegram-e2e
# Copy only if no private configuration exists already.
cp -n tools/telegram_e2e/config.example.toml private/telegram-e2e/config.toml
chmod 600 private/telegram-e2e/config.toml
```

Create a dedicated bot through Telegram's verified @BotFather. Obtain user-client `api_id` and `api_hash` through [Telegram application registration](https://core.telegram.org/api/obtaining_api_id). Set the bot token, numeric bot ID (token prefix) and username. Set `dedicated_test_bot = true` only after confirming this is the dedicated test bot. Prefer a separate user account; the runner authenticates that user and permits only its private chat on the test instance.

Run from the repository directory:

```sh
uv run --group e2e python -m tools.telegram_e2e.cli login
```

Enter the phone number, login code and any two-step password in your own terminal. They are hidden during entry. The command prints the authenticated numeric user ID locally; put that number in `user_id`. Subsequent runs reuse `private/telegram-e2e/user.session`. Treat that file as an account credential; it must remain private. The runner does not automatically log in during tests. A revoked session requires another interactive login.

## Execute

```sh
uv run --group e2e python -m tools.telegram_e2e.cli doctor
uv run --group e2e python -m tools.telegram_e2e.cli run smoke
uv run --group e2e python -m tools.telegram_e2e.cli run regression
uv run --group e2e python -m tools.telegram_e2e.cli run supplements
uv run --group e2e python -m tools.telegram_e2e.cli run nutrients
```

`doctor` verifies the user, bot identity and absence of a webhook without sending messages. `smoke` covers start/status/Refresh and a measured meal with truthful unknown nutrients. `regression` additionally covers rough-draft revisions, stale/repeated approval, reply correction, editing an original message, delete/undo and a worker restart. It also includes supplement reporting, plan adherence and nutrient coverage (ten scenarios total). The focused `supplements` suite runs the two supplement scenarios; `nutrients` tests reference selection and uncertainty across ten explicitly completed food days.

The current application deliberately responds to an edited original message with guidance and leaves the diary unchanged. The scenario verifies that existing behavior; replying to the receipt is the implemented correction path.

Every scenario sends real Telegram messages containing synthetic foods. Run only when you have exclusive use of this bot and test account: do not manually chat with it during a run or run another instance with its token elsewhere. A local lock prevents overlapping runner/login processes on this checkout. The controller drains old updates before starting the worker; chat history is retained by Telegram. The test database is removed after the worker stops, including on ordinary failures. No production database is opened by the runner. The local production bot ID is additionally rejected if configured in the project's `.env`.

The runner stops on the first failure. Waits are bounded and mutating steps are not automatically retried. Telegram flood waits stop the run with the required wait duration; wait that long before rerunning. Correct the cause, then rerun the suite to get fresh state. No fixed Telegram request quota is assumed.

## Results

The command prints the private report path under `private/telegram-e2e/runs/`. Each run writes JSON, HTML and JUnit XML with the source fingerprint, fixture version, action/expected-response timeline, callback acknowledgements and redacted received text. Status distinguishes assertion, setup, infrastructure and runner failures. A failed live run exits unsuccessfully. Reports remain local until you remove them; automatic retention pruning is not implemented in this first version.

No API hashes, bot tokens or user IDs should appear in reports. Do not publish session files, reports, chat transcripts or screenshots. Completed scenario databases are removed rather than archived. A process killed without cleanup may leave a disposable `state-*` directory in that run's folder; stop any surviving test worker before removing it. Graceful interruption and normal failures run cleanup.

## Offline development checks

```sh
uv run --group e2e python -m pytest tools/telegram_e2e/tests -q
uv run ruff check .
uv run mypy
```

Normal `uv run pytest` still selects only `tests/` and never sends Telegram messages. The runner's offline tests cover identity/path/credential guards, event races, same-ID edits, duplicate responses, non-retried sends, flood waits, callback handling, redaction, report classification, process cleanup and migrated fixture state. Nine scenario contracts also execute against the real application service using an offline Telegram double. These are not live Telegram acceptance evidence.

The initial implementation supports a locally managed subprocess worker and text/button scenarios. Container orchestration, screenshots, media, CI scheduling and broader feature suites remain extensions. Existing application tests continue to cover injected failures and exact duplicate update IDs that cannot be reliably generated through a real user client.

See [the architecture proposal](telegram-e2e-design.md) for rationale and trade-offs.

## Verification evidence — 27 September 2026

Authenticated user and bot identity checks passed against a dedicated test bot. The two-scenario live smoke suite passed, followed by all seven live regression scenarios (including those smoke checks again). Real Telegram callbacks verified stale/repeated draft approval, receipt correction, original-message edit guidance, delete/undo and restart persistence. Disposable scenario databases were removed, and the reports were checked for configured secrets and account identifiers. Exact reports remain in ignored private storage.

The live rollout exposed a pytest startup collision between the short asyncio plugin name and the already imported standard-library module. The CLI now loads the full plugin module name; an offline bootstrap regression covers it. All 28 runner tests, Ruff and strict mypy pass. This evidence covers the listed scenarios, not the entire planned bot feature set.
