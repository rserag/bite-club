import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from tools.telegram_e2e.config import ROOT, Settings


@dataclass
class Recorder:
    settings: Settings
    started: float = field(default_factory=time.monotonic)
    events: list[dict[str, object]] = field(default_factory=list)

    def clean(self, text: str) -> str:
        for value in (
            self.settings.bot_token.get_secret_value(),
            self.settings.api_hash.get_secret_value(),
            self.settings.bot_username,
        ):
            text = text.replace(value, "[private]")
        for identifier in (self.settings.user_id, self.settings.bot_id, self.settings.api_id):
            text = re.sub(rf"(?<!\d){identifier}(?!\d)", "[id]", text)
        text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]+", "[token]", text)
        return text[:4000]

    def add(self, kind: str, detail: str) -> None:
        self.events.append(
            {
                "seconds": round(time.monotonic() - self.started, 3),
                "kind": kind,
                "detail": self.clean(detail),
            }
        )


def fingerprint() -> str:
    digest = hashlib.sha256()
    paths = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for folder in ("src", "migrations", "tools/telegram_e2e"):
        paths.extend((ROOT / folder).rglob("*.py"))
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def write_report(output: Path, results: list[dict[str, object]]) -> None:
    body = {
        "schema": 1,
        "source": fingerprint(),
        "fixtures": "synthetic-foods-v1",
        "environment": "local-real-telegram",
        "results": results,
    }
    (output / "report.json").write_text(json.dumps(body, indent=2))
    (output / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Telegram E2E results</title>'
        "<style>body{font:16px system-ui;max-width:1000px;margin:40px auto}"
        "pre{white-space:pre-wrap;background:#f5f5f5;padding:20px}</style>"
        "<h1>Telegram E2E results</h1><pre>" + html.escape(json.dumps(body, indent=2)) + "</pre>"
    )
    suite = Element("testsuite", name="telegram-e2e", tests=str(len(results)))
    for row in results:
        case = SubElement(suite, "testcase", name=str(row["scenario"]))
        if row["status"] != "passed":
            tag = "failure" if row["status"] == "assertion_failure" else "error"
            SubElement(case, tag, message=str(row["status"])).text = str(row.get("detail", ""))
    (output / "junit.xml").write_bytes(tostring(suite, encoding="utf-8", xml_declaration=True))
