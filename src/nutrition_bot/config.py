from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "sqlite+aiosqlite:///./runtime-data/app.sqlite3"
    app_timezone: str = "UTC"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    raw_input_retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator("database_url")
    @classmethod
    def local_database(cls, value: str) -> str:
        url = make_url(value)
        if (
            url.drivername != "sqlite+aiosqlite"
            or not url.database
            or url.database == ":memory:"
            or url.host
            or url.username
            or url.password
            or url.query
        ):
            raise ValueError(
                "Use a local file-backed sqlite+aiosqlite URL without query parameters"
            )
        return value

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone") from exc
        return value

    @property
    def database_path(self) -> Path:
        database = make_url(self.database_url).database
        assert database is not None
        return Path(database).expanduser().resolve()

    @property
    def resolved_database_url(self) -> str:
        return make_url(self.database_url).set(database=str(self.database_path)).render_as_string()


class NutritionSourceSettings(StorageSettings):
    usda_api_key: SecretStr | None = None
    usda_timeout_seconds: float = Field(default=10, ge=1, le=30, allow_inf_nan=False)
    openfoodfacts_enabled: bool = False

    @field_validator("usda_api_key", mode="before")
    @classmethod
    def empty_key_is_disabled(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class BotSettings(NutritionSourceSettings):
    telegram_bot_token: SecretStr
    allowed_telegram_user_id: int = Field(gt=0)
    allowed_telegram_chat_id: int = Field(gt=0)

    llm_provider: Literal["disabled", "openrouter", "chatgpt"] = "disabled"
    openrouter_api_key: SecretStr | None = None
    ai_endpoint_manifest: Path | None = None
    chatgpt_credentials_path: Path | None = None
    chatgpt_policy_path: Path | None = None
    miniapp_enabled: bool = False
    miniapp_host: str = "127.0.0.1"
    miniapp_port: int = Field(default=8080, ge=1024, le=65535)
    miniapp_url: str | None = None

    @field_validator("miniapp_url")
    @classmethod
    def https_miniapp(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Use an HTTPS Mini App URL without credentials or query parameters")
        return value

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def empty_ai_key(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("telegram_bot_token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        from aiogram.utils.token import validate_token

        try:
            validate_token(value.get_secret_value())
        except Exception as exc:
            raise ValueError("Invalid Telegram bot token") from exc
        return value

    @property
    def bot_id(self) -> int:
        return int(self.telegram_bot_token.get_secret_value().split(":", 1)[0])
