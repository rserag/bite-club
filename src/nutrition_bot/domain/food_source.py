"""Replaceable food-source contract; candidates never imply consumed meals."""

import re
from typing import Literal, Protocol

from pydantic import Field, model_validator

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
    provider: Literal["usda", "openfoodfacts"] = "usda"


class SourceDocument(FrozenModel):
    source_id: str
    record: ReviewedFoodInput
    data_type: str
    source_published_date: str | None = None
    adapter_version: str
    warnings: tuple[str, ...] = ()
    provider: Literal["usda", "openfoodfacts"] = "usda"


class SourceProvenance(FrozenModel):
    source_kind: Literal["usda", "openfoodfacts"] = "usda"
    external_id: str = Field(min_length=1, max_length=14)
    fetched_at: float = Field(gt=0, allow_inf_nan=False)
    published_date: str | None = None
    adapter_version: str
    data_type: str
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> "SourceProvenance":
        if self.source_kind == "usda":
            if not re.fullmatch(r"[1-9][0-9]{0,11}", self.external_id):
                raise ValueError("Invalid USDA food identifier")
        else:
            from nutrition_bot.domain.barcodes import normalize_barcode

            if normalize_barcode(self.external_id) != self.external_id:
                raise ValueError("Use the normalized product barcode")
        return self


class FoodProvider(Protocol):
    async def search(self, query: str) -> tuple[FoodCandidate, ...]: ...

    async def fetch(self, source_id: str) -> SourceDocument: ...
