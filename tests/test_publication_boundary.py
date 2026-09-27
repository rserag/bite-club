"""Source publication guard regressions use deliberately synthetic strings only."""

import pytest

from scripts.audit_publication import inspect


@pytest.mark.parametrize(
    "path",
    [
        "private/config.toml",
        ".beads/issues.jsonl",
        ".codex/hooks.json",
        ".agents/settings.yaml",
        "config.yaml",
        "notes/.env.production",
        "data/app.sqlite3",
        "data/app.sqlite3-wal",
        "session/user.session-journal",
        "exports/data.json",
        "backups/archive.tar.gz",
        "keys/signing.pem",
        "logs/worker.log",
    ],
)
def test_private_artifacts_are_rejected(path):
    assert "private-file" in inspect(path, b"synthetic")


def test_credentials_are_rejected_without_printing_values():
    assert inspect("src/example.py", b"123456:" + b"A" * 35) == ["telegram-token"]
    assert inspect("README.md", b"ghp_" + b"A" * 35) == ["github-token"]
    assert inspect("docs/example.md", b"-----BEGIN " + b"PRIVATE KEY-----") == ["private-key"]
    assert inspect("docs/example.md", b"/Users/" + b"synthetic/private/config") == ["personal-home"]


def test_only_exact_dummy_credentials_in_fixture_paths_are_allowed():
    dummy = b"123456:synthetic_token_for_offline_tests_only"
    assert inspect("tests/conftest.py", dummy) == []
    assert inspect("src/example.py", dummy) == ["telegram-token"]
    assert inspect("tests/example.py", b"123456:" + b"B" * 35) == ["telegram-token"]
    assert inspect(".env.example", b"TELEGRAM_BOT_TOKEN=\n") == []
    assert inspect("tools/telegram_e2e/config.example.toml", b'bot_token = "REPLACE_ME"') == []
