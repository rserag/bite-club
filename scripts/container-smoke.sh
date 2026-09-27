#!/usr/bin/env bash
set -euo pipefail
image_name="${1:-nutrition-bot:local}"
volume_name="nutrition-bot-smoke-${RANDOM}-$$"
docker volume create "$volume_name" >/dev/null
trap 'docker volume rm "$volume_name" >/dev/null' EXIT
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp \
  --mount "type=volume,src=$volume_name,dst=/data" \
  "$image_name" migrate
# A second fresh container sees the same schema and may repeat migrations safely.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp \
  --mount "type=volume,src=$volume_name,dst=/data" \
  --entrypoint python "$image_name" -c '
import os
import ssl
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from nutrition_bot.cli import migrate
from nutrition_bot.config import StorageSettings
from nutrition_bot.runtime.backup import snapshot, verify_snapshot
from nutrition_bot.runtime.health import check_health
assert os.getuid() != 0
settings = StorageSettings()
migrate(settings)
assert check_health(settings).reason == "worker_stale"
assert settings.database_path.is_file()
assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0
assert datetime(2026, 1, 1, tzinfo=ZoneInfo("America/New_York")).utcoffset() == timedelta(hours=-5)
assert datetime(2026, 7, 1, tzinfo=ZoneInfo("America/New_York")).utcoffset() == timedelta(hours=-4)
backup = Path("/data/smoke-backup")
manifest = snapshot(settings.database_path, backup, "sha256:" + "0" * 64)
assert verify_snapshot(backup) == manifest
print("Non-root persistence, migrations, backup, TLS roots and timezone smoke test passed.")
'
