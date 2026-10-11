"""Source publication guard regressions use deliberately synthetic strings only."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.audit_publication import PATTERNS, REVIEWED_JPEGS, inspect, reviewed_huffman_ranges

ROOT = Path(__file__).resolve().parents[1]
PHOTO_NAME = "evals/fixtures/meal-photo-01.jpg"


def jpeg_segment(marker, payload):
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def pin_modified_photo(monkeypatch, body):
    # Even a newly reviewed hash must not exempt metadata or arbitrary tables.
    monkeypatch.setitem(REVIEWED_JPEGS, PHOTO_NAME, hashlib.sha256(body).hexdigest())


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


@pytest.mark.parametrize("name", REVIEWED_JPEGS)
def test_exact_reviewed_jpegs_only_exempt_standard_huffman_false_positive(name):
    body = (ROOT / name).read_bytes()
    assert hashlib.sha256(body).hexdigest() == REVIEWED_JPEGS[name]
    assert len(list(PATTERNS["telegram-token"].finditer(body))) == 1
    assert reviewed_huffman_ranges(name, body)
    assert inspect(name, body) == []


def test_reviewed_hashes_match_synthetic_visual_review_manifest():
    manifest = json.loads((ROOT / "evals/fixtures/meal-photos.json").read_text())
    assert manifest["synthetic"] is True
    assert all(row["synthetic"] and row["agent_visual_review"] for row in manifest["images"])
    assert REVIEWED_JPEGS == {
        f"evals/fixtures/{row['file']}": row["sha256"] for row in manifest["images"]
    }


@pytest.mark.parametrize("name", ["evals/unreviewed.jpg", "data/unknown.bin", "private/photo.jpg"])
def test_renamed_private_and_unknown_binary_files_have_no_exception(name):
    body = (ROOT / PHOTO_NAME).read_bytes()
    assert "telegram-token" in inspect(name, body)
    if name.startswith("private/"):
        assert "private-file" in inspect(name, body)


def test_changed_fixture_fails_closed_without_reencoding_or_updating_manifest():
    body = (ROOT / PHOTO_NAME).read_bytes() + b"changed snapshot"
    assert reviewed_huffman_ranges(PHOTO_NAME, body) == []
    assert inspect(PHOTO_NAME, body) == ["telegram-token"]


@pytest.mark.parametrize("marker", [0xE0, 0xE1, 0xED, 0xFE, None])
@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        (b"123456:" + b"A" * 35, "telegram-token"),
        (b"ghp_" + b"A" * 35, "github-token"),
        (b"-----BEGIN " + b"PRIVATE KEY-----", "private-key"),
        (b"/Users/" + b"synthetic/private/config", "personal-home"),
    ],
)
def test_reviewed_image_still_scans_metadata_and_trailing_content(
    monkeypatch, marker, secret, kind
):
    original = (ROOT / PHOTO_NAME).read_bytes()
    if marker is None:
        body = original + b"\n" + secret
    else:
        body = original[:2] + jpeg_segment(marker, secret) + original[2:]
    pin_modified_photo(monkeypatch, body)
    assert reviewed_huffman_ranges(PHOTO_NAME, body)
    assert inspect(PHOTO_NAME, body) == [kind]


def test_token_in_nonstandard_huffman_table_is_rejected_even_for_reviewed_hash(monkeypatch):
    original = (ROOT / PHOTO_NAME).read_bytes()
    body = original[:2] + jpeg_segment(0xC4, b"123456:" + b"A" * 35) + original[2:]
    pin_modified_photo(monkeypatch, body)
    assert reviewed_huffman_ranges(PHOTO_NAME, body)
    assert inspect(PHOTO_NAME, body) == ["telegram-token"]


@pytest.mark.parametrize("damage", ["no-soi", "short-segment", "oversized-segment", "no-scan"])
def test_malformed_jpeg_headers_have_no_exception_even_for_reviewed_hash(monkeypatch, damage):
    original = (ROOT / PHOTO_NAME).read_bytes()
    if damage == "no-soi":
        body = b"xx" + original[2:]
    elif damage == "short-segment":
        body = original[:2] + b"\xff\xe1\x00\x01" + original[2:]
    elif damage == "oversized-segment":
        body = original[:2] + b"\xff\xe1\xff\xff" + original[2:700]
    else:
        body = original[:700]
    pin_modified_photo(monkeypatch, body)
    assert reviewed_huffman_ranges(PHOTO_NAME, body) == []
    assert inspect(PHOTO_NAME, body) == ["telegram-token"]
