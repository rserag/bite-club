"""Replaceable food-source contract; candidates never imply consumed meals."""

from typing import Literal, Protocol

from pydantic import Field

from nutrition_bot.domain.food import FrozenModel, Preparation, ReviewedFoodInput, Text

SourceErrorCode = Literal[
    "not_configured",
    "timeout",
    "rate_limited",
    "unavailable",
    "not_found",
    "invalid_response",
    "unsupported_food",
]


class ProviderError(Exception):
    def __init__(self, code: SourceErrorCode, retry_after_seconds: int | None = None):
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


class FoodCandidate(FrozenModel):
    source_id: str
    name: Text
    data_type: str
    preparation_hint: Preparation
    brand: Text | None = None


class SourceDocument(FrozenModel):
    source_id: str
    record: ReviewedFoodInput
    data_type: str
    source_published_date: str | None = None
    adapter_version: str
    warnings: tuple[str, ...] = ()


class SourceProvenance(FrozenModel):
    source_kind: Literal["usda"] = "usda"
    external_id: str = Field(pattern=r"^[1-9][0-9]{0,11}$")
    fetched_at: float = Field(gt=0, allow_inf_nan=False)
    published_date: str | None = None
    adapter_version: str
    data_type: str
    warnings: tuple[str, ...] = ()


class FoodProvider(Protocol):
    async def search(self, query: str) -> tuple[FoodCandidate, ...]: ...

    async def fetch(self, source_id: str) -> SourceDocument: ...
