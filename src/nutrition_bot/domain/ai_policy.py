"""Operator-reviewed endpoint policy and conservative integer cost ceilings."""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from nutrition_bot.domain.ai import AiRole, AiUnavailable
from nutrition_bot.domain.food import FrozenModel

POLICY_MAX_AGE_DAYS = 30


class ReviewedAiRoute(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    role: AiRole
    model: str = Field(pattern=r"^[a-z0-9_-]+/[a-z0-9._-]+$")
    provider_slug: str = Field(pattern=r"^[a-z0-9._/-]{1,100}$")
    response_provider_names: tuple[str, ...] = Field(min_length=1, max_length=5)
    policy_url: str = Field(pattern=r"^https://[^\s]{1,500}$")
    endpoint_metadata_url: str = Field(pattern=r"^https://openrouter\.ai/[^\s]{1,500}$")
    pricing_url: str = Field(pattern=r"^https://[^\s]{1,500}$")
    reviewed_on: date
    expires_on: date
    training_prohibited: Literal[True]
    retention_purpose: str = Field(min_length=1, max_length=500)
    retention_duration: str = Field(min_length=1, max_length=200)
    modalities: tuple[Literal["text", "image"], ...] = Field(min_length=1, max_length=2)
    parameters: tuple[str, ...] = Field(min_length=1, max_length=20)
    input_micro_usd_per_million: int = Field(strict=True, ge=0, le=100_000_000)
    output_micro_usd_per_million: int = Field(strict=True, ge=0, le=100_000_000)
    request_micro_usd: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    image_micro_usd: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    max_input_tokens: int = Field(strict=True, ge=1, le=12_000)
    max_output_tokens: int = Field(strict=True, ge=1, le=2000)
    image_input_token_ceiling: int = Field(default=0, strict=True, ge=0, le=12_000)
    image_token_bound_source: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def capabilities(self) -> "ReviewedAiRoute":
        required = {"response_format", "max_tokens", "temperature"}
        if not required.issubset(self.parameters) or "text" not in self.modalities:
            raise ValueError("The reviewed endpoint must support every requested parameter.")
        if self.role == "meal_photo" and (
            "image" not in self.modalities
            or self.image_input_token_ceiling < 1
            or not self.image_token_bound_source
        ):
            raise ValueError("Image routes need a documented conservative image token bound.")
        if not 0 <= (self.expires_on - self.reviewed_on).days <= POLICY_MAX_AGE_DAYS:
            raise ValueError("Endpoint reviews expire within 30 days.")
        if self.reservation_micro_usd < 1:
            raise ValueError("A paid endpoint requires a conservative positive reservation.")
        return self

    @property
    def reservation_micro_usd(self) -> int:
        token_cost = (
            self.max_input_tokens * self.input_micro_usd_per_million
            + self.max_output_tokens * self.output_micro_usd_per_million
            + 999_999
        ) // 1_000_000
        return token_cost + self.request_micro_usd + self.image_micro_usd

    def check_date(self, today: date) -> None:
        if not self.reviewed_on <= today <= self.expires_on:
            raise AiUnavailable("The AI endpoint review is expired or not active yet.")


class AiEndpointManifest(FrozenModel):
    version: Literal[1]
    reviewed_on: date
    account_training_opt_out_confirmed: bool = Field(strict=True)
    gateway_content_logging_disabled: bool = Field(strict=True)
    dedicated_key_confirmed: bool = Field(strict=True)
    auto_top_up_disabled: bool = Field(strict=True)
    routes: tuple[ReviewedAiRoute, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def safe_account(self) -> "AiEndpointManifest":
        if self.routes and not all(
            (
                self.account_training_opt_out_confirmed,
                self.gateway_content_logging_disabled,
                self.dedicated_key_confirmed,
                self.auto_top_up_disabled,
            )
        ):
            raise ValueError("Reviewed routes require the documented account privacy controls.")
        if len({route.id for route in self.routes}) != len(self.routes):
            raise ValueError("Endpoint route identifiers must be unique.")
        if len({route.role for route in self.routes}) != len(self.routes):
            raise ValueError("Use one explicit endpoint per task; no automatic fallback routing.")
        return self

    def route(self, role: AiRole, today: date) -> ReviewedAiRoute:
        match = next((route for route in self.routes if route.role == role), None)
        if match is None:
            raise AiUnavailable("No reviewed AI endpoint is configured for this task.")
        match.check_date(today)
        return match


def load_manifest(path: Path) -> AiEndpointManifest:
    try:
        if path.stat().st_size > 65_536:
            raise AiUnavailable("The AI endpoint manifest is too large.")
        return AiEndpointManifest.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        raise AiUnavailable("The AI endpoint manifest is unavailable or invalid.") from None
