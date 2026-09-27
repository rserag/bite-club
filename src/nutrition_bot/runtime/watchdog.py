"""Operator-side single-container stall check; never runs inside the bot container."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nutrition_bot.runtime.lock import database_lock


class WatchState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    generation: str = ""
    first_stale: float | None = None
    checks: int = Field(default=0, ge=0)
    checked_at: float = 0
    restarted_at: float | None = None


def decision(
    state: WatchState, *, now: float, generation: str, running: bool, reason: str
) -> tuple[WatchState, str]:
    next_state = state.model_copy(deep=True)
    if (
        generation != state.generation
        or not 0 <= now - state.checked_at <= 120
        or not running
        or reason != "worker_stale"
    ):
        next_state.first_stale = None
        next_state.checks = 0
    next_state.generation = generation
    next_state.checked_at = now
    if not running or reason != "worker_stale":
        return next_state, "no_restart"
    if next_state.first_stale is None:
        next_state.first_stale = now
    next_state.checks += 1
    if next_state.checks < 3 or now - next_state.first_stale < 180:
        return next_state, "observe_stall"
    if next_state.restarted_at is not None and now - next_state.restarted_at < 900:
        return next_state, "restart_cooldown"
    return next_state, "restart_needed"


def docker(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def container_state(container: str) -> tuple[bool, str]:
    result = docker(["inspect", "--format", "{{json .State}}", container])
    if result.returncode != 0:
        raise RuntimeError("Container inspection failed")
    value = json.loads(result.stdout)
    return value.get("Running") is True and value.get("Restarting") is False, value["StartedAt"]


def save_state(path: Path, state: WatchState) -> None:
    temporary = path.with_name(path.name + ".tmp")
    # The state lock serializes calls, including the write before a restart attempt.
    with temporary.open("w") as stream:
        temporary.chmod(0o600)
        stream.write(state.model_dump_json())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def check(container: str, state_file: Path, deployment_lock: Path, *, restart: bool) -> str:
    if not container or container.startswith("-"):
        raise ValueError("Use an explicit container name or ID")
    with database_lock(deployment_lock), database_lock(state_file):
        previous = (
            WatchState.model_validate_json(state_file.read_text())
            if state_file.exists()
            else WatchState()
        )
        running, generation = container_state(container)
        reason = "container_stopped"
        if running:
            result = docker(["exec", container, "python", "-m", "nutrition_bot", "healthcheck"])
            if result.returncode not in (0, 1):
                raise RuntimeError("Health command failed")
            value = json.loads(result.stdout)
            reason = value["reason"]
            if not isinstance(reason, str):
                raise ValueError("Invalid health response")
        state, action = decision(
            previous, now=time.time(), generation=generation, running=running, reason=reason
        )
        if action == "restart_needed" and restart:
            if container_state(container) != (True, generation):
                state.first_stale, state.checks = None, 0
                save_state(state_file, state)
                return "container_changed"
            # Reserve the cooldown durably even if Docker fails or the command is interrupted.
            state.restarted_at = time.time()
            state.first_stale, state.checks = None, 0
            save_state(state_file, state)
            if docker(["restart", "--time", "20", container]).returncode != 0:
                raise RuntimeError("Restart failed; cooldown retained")
            return "restart_requested"
        save_state(state_file, state)
        if reason in {"disk_low", "database_unreadable", "database_missing", "schema_mismatch"}:
            return "attention_" + reason
        return action


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--deployment-lock", type=Path, required=True)
    parser.add_argument("--restart", action="store_true", help="Permit a bounded stall restart")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(
            json.dumps(
                {
                    "status": check(
                        args.container, args.state_file, args.deployment_lock, restart=args.restart
                    )
                }
            )
        )
    except Exception as exc:
        # Docker output and exception messages can contain private operator paths.
        print(json.dumps({"error": "watchdog_check_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
