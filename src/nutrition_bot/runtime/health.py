import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

from nutrition_bot.adapters.database.schema import SCHEMA_REVISION
from nutrition_bot.config import StorageSettings

COMPONENTS = ("receiver", "processor", "sender", "cleanup", "scheduler")


@dataclass(frozen=True)
class Health:
    healthy: bool
    reason: str
    degraded: tuple[str, ...] = ()


def check_health(settings: StorageSettings, max_age: float = 90) -> Health:
    if not settings.database_path.is_file():
        return Health(False, "database_missing")
    try:
        if shutil.disk_usage(settings.database_path.parent).free < 64 * 1024 * 1024:
            return Health(False, "disk_low")
        with closing(
            sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if revision != (SCHEMA_REVISION,):
                return Health(False, "schema_mismatch")
            rows = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT component, touched_at, state FROM runtime_heartbeat"
                )
            }
    except (sqlite3.Error, OSError):
        return Health(False, "database_unreadable")
    now = time.time()
    if any(row[1] == "stopped" for row in rows.values()):
        return Health(False, "worker_stopped")
    for component in COMPONENTS:
        row = rows.get(component)
        if not row or not 0 <= now - row[0] <= max_age:
            return Health(False, "worker_stale")
    degraded = tuple(name for name in COMPONENTS if rows[name][1] == "degraded")
    return Health(True, "running", degraded)
