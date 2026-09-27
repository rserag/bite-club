# Engineering Bite Club

The interface is a chat. The hard part is making that chat a trustworthy record.

## Correctness before interpretation

The implemented path is deterministic: parse a supported command, resolve a reviewed food version, calculate with explicit units, and persist a revision. Food values have provenance. A missing value is not zero. Approved quantity estimates remain estimates after saving.

The planned AI layer can propose interpretations and drafts; it cannot bypass approval or become an authoritative nutrition database. The [synthetic evaluation seed](../evals/README.md) distinguishes extraction quality from guarantees enforced by application code. That work is specified, not presented as an activated AI feature or a measured benchmark.

## A chat message is not a transaction

Telegram can replay an update. A user can press an old button. A process can die after sending a message but before recording its delivery. The runtime uses a durable inbox, action identities, expected revisions and a transactional outbox to make each application action idempotent.

The guarantee ends at the network boundary. A reply can be duplicated after an ambiguous send; a ledger action must not be. This trade-off is explicit in [operations](foundation-operations.md) and exercised by the [crash test](../tests/test_process_crash.py) and [transport tests](../tests/test_transport.py).

## Corrections without rewriting evidence

Food labels, recipes, meal quantities, targets and supplement doses have versioned representations. Accepting a new version does not rewrite the source of old consumption. Reports follow the current accepted revision; historical evidence remains available for correction and undo. SQLite constraints and triggers enforce invariants in addition to application validation.

A rough meal is a separate persisted draft. Approval identifies its displayed revision. Editing the draft invalidates an older approval path. Reusing a meal with an estimated portion creates a fresh approval request, rather than laundering the estimate into a measured amount.

## Honest analytics

Averages use eligible complete days, with the denominator shown. Weight and training reviews require enough coverage to support their specific comparison. Missing training attendance is not rest; scheduled doses are not intake; creatine exposure is not a micronutrient gap.

The nutrient reference bundle records scope, units, kind, version and source. RDA, AI, CDRR-style limits and ULs answer different questions. The report cannot substitute one for another. Unknown foods that could materially change a result block a gap message even when a numeric coverage threshold is satisfied.

## A deliberately small runtime

One Python process and local SQLite fit the single-user workload. Long polling needs no public ingress, webhook server, message broker or distributed coordination layer. A process lock prevents competing local workers; WAL and short transactions keep the write model understandable.

This is a scope choice, not a claim that SQLite solves every scaling problem. Multi-user tenancy would need explicit isolation. Horizontal workers would need a different coordination model. Builds and exhaustive tests also need more resources than the lightweight runtime itself.

## Test through several boundaries

| Layer | Evidence |
| --- | --- |
| Pure domain | Unit/amount conversions, temporal rules, arithmetic boundaries and unknown-data behavior |
| Database | Real migrations, immutable snapshots, constraints, rollback and replay |
| Application | Actual service receiving synthetic Telegram updates and callbacks |
| Process/container | Hard termination, recovery, non-root execution and persistent volumes |
| Live transport | Telethon user → dedicated Telegram test bot → disposable real worker/database |

The live runner isolates state for each scenario and rejects accidental production configuration. Responses and ledger changes are both checked. It reports assertion failures separately from infrastructure/setup failures, and keeps transcripts and sessions private. An offline Telegram double executes the same scenario assertions without network access.

The full ten-scenario live suite is available for deliberate runs; ordinary CI executes the offline suite and container checks. A passing subset of live tests is evidence for those paths, not a claim that every feature has live acceptance coverage.

## Development process

This project was developed iteratively with AI coding assistance. Product scope, architecture trade-offs, data invariants and acceptance criteria are written down; generated code is subject to the same tests and source review as other changes. The public repository preserves code, design reasoning, synthetic fixtures and reproducible checks. It excludes personal conversations, maintainer-local tracker history, credentials and deployment records.

A useful example of that workflow is [connection cancellation](../tests/test_database_cancellation.py): an intermittent resource warning was converted into a deterministic reproduction, then a bounded ownership fix and regression cases. The intent is to show reasoning and evidence, not just a large test count.

## Known boundaries

No paid AI interpretation, photo ingestion, reminders, public multi-user service, automatic encrypted backup pipeline or automated deployment is active. The future design includes those features, but their presence in a proposal is not completion. Nutrient reports are not diagnosis or safety clearance. Runtime operators must independently verify their backup/restore process before relying on important records.
