# Security and privacy

Bite Club is a single-user application. Access is restricted to an explicitly configured Telegram user and private chat. It is not designed as a shared multi-tenant service.

## Reporting a vulnerability

Do not publish credentials or personal records in an issue. Use GitHub's private vulnerability reporting option if it is available on this repository. Otherwise contact the maintainer through the public GitHub profile to arrange a private channel before sharing sensitive details. Include a minimal synthetic reproduction, affected version and expected behavior.

No response-time or security-support SLA is promised. If a token or Telegram user-session file is exposed, revoke the affected credential/session through the relevant provider; deleting a public file alone does not revoke access.

## Operating boundaries

- Telegram processes bot conversations. Self-hosting controls application storage, not Telegram's data handling.
- Telethon session files grant account access. Keep them private and use dedicated test accounts.
- Keep `.env`, runtime databases, logs, photos, exports, backups and deployment inventories out of source control and CI artifacts.
- An allowlist is not a tenant boundary. Do not add another user to a production diary as a test strategy.
- SQLite needs a local persistent filesystem and an exclusive worker. Follow the backup/migration runbook.
- The current application has no active paid AI integration. Future AI routes require explicit privacy and budget controls.

CI uses synthetic data and no production credentials. Live end-to-end testing and deployment are deliberate operator actions, not pull-request workflows.
