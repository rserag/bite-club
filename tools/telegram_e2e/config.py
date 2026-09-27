import stat
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "private" / "telegram-e2e"


class SetupError(Exception):
    """Safe fixed-code setup error, never a raw provider exception."""


class InfrastructureError(Exception):
    """Transport or worker failure, distinct from a failed product assertion."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    dedicated_test_bot: bool
    api_id: int = Field(gt=0)
    api_hash: SecretStr
    bot_token: SecretStr
    bot_id: int = Field(gt=0)
    bot_username: str = Field(pattern=r"^[A-Za-z0-9_]{5,32}$")
    user_id: int = Field(ge=0)
    step_timeout: float = Field(default=30, ge=5, le=60)
    pace_seconds: float = Field(default=1.2, ge=1, le=10)

    @model_validator(mode="after")
    def identities(self) -> "Settings":
        from aiogram.utils.token import validate_token

        if not self.dedicated_test_bot:
            raise ValueError("dedicated_test_bot_required")
        token = self.bot_token.get_secret_value()
        validate_token(token)
        if token.split(":", 1)[0] != str(self.bot_id):
            raise ValueError("bot_identity_mismatch")
        if len(self.api_hash.get_secret_value()) != 32:
            raise ValueError("invalid_api_hash")
        return self


def confined(path: Path, root: Path) -> Path:
    """Reject symlinks, including symlinked ancestors, and traversal out of root."""
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise SetupError("symlink_path_rejected")
    resolved = absolute.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise SetupError("path_outside_test_root")
    return resolved


def load(path: Path, *, root: Path = PRIVATE, allow_unknown_user: bool = False) -> Settings:
    path = confined(path, root)
    if not path.is_file():
        raise SetupError("configuration_missing")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SetupError("configuration_requires_mode_0600")
    try:
        settings = Settings.model_validate(tomllib.loads(path.read_text()))
    except Exception:
        raise SetupError("invalid_test_configuration") from None
    # Additional protection against accidentally copying the local production token.
    from dotenv import dotenv_values

    production = dotenv_values(ROOT / ".env").get("TELEGRAM_BOT_TOKEN")
    if production and production.split(":", 1)[0] == str(settings.bot_id):
        raise SetupError("production_bot_rejected")
    if not settings.user_id and not allow_unknown_user:
        raise SetupError("user_id_required_run_login_first")
    return settings
