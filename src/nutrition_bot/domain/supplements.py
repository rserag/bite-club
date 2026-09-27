import json
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nutrition_bot.domain.food import MAX_INTEGER, exact_decimal

ReferenceKind = Literal["RDA", "AI", "limit", "UL"]
ReferenceApplicability = Literal["food", "supplement", "fortified_and_supplement", "total"]
SubstanceCategory = Literal["nutrient", "performance_compound", "other"]
SupplementUnit = Literal["g", "mg", "ug", "IU", "CFU", "mL"]
SUPPLEMENT_AMOUNT_SCALE = 1_000_000


def supplement_amount_scaled(
    amount: Decimal | str | int, source: SupplementUnit, canonical: SupplementUnit
) -> int:
    value = exact_decimal(amount)
    if source == canonical:
        normalized = value
    elif source in {"g", "mg", "ug"} and canonical in {"g", "mg", "ug"}:
        mass_units = {"g": Decimal(1), "mg": Decimal("0.001"), "ug": Decimal("0.000001")}
        normalized = value * mass_units[source] / mass_units[canonical]
    else:
        raise ValueError("A reviewed form-specific conversion is required")
    with localcontext() as context:
        context.prec = 50
        stored = (normalized * SUPPLEMENT_AMOUNT_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)
    if stored > MAX_INTEGER:
        raise ValueError("Supplement amount exceeds storage range")
    return int(stored)


class FrozenReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class ProtocolPhase(FrozenReference):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    duration_days_min: int | None = Field(default=None, ge=1, le=365)
    duration_days_max: int | None = Field(default=None, ge=1, le=365)
    daily_amount_mg_min: int | None = Field(default=None, ge=1, le=100_000)
    daily_amount_mg_max: int | None = Field(default=None, ge=1, le=100_000)
    mg_per_kg: int | None = Field(default=None, ge=1, le=2_000)
    frequency: int = Field(ge=1, le=24)
    per_serving_mg: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_phase(self) -> "ProtocolPhase":
        fixed = self.daily_amount_mg_min is not None or self.daily_amount_mg_max is not None
        if fixed == (self.mg_per_kg is not None):
            raise ValueError("Use either a fixed daily range or a weight-based amount")
        if fixed:
            if self.daily_amount_mg_min is None or self.daily_amount_mg_max is None:
                raise ValueError("A fixed daily range needs both bounds")
            if self.daily_amount_mg_max < self.daily_amount_mg_min:
                raise ValueError("Daily amount bounds are reversed")
        if (self.duration_days_min is None) != (self.duration_days_max is None):
            raise ValueError("Duration needs both bounds or neither")
        if (
            self.duration_days_min is not None
            and self.duration_days_max is not None
            and self.duration_days_max < self.duration_days_min
        ):
            raise ValueError("Duration bounds are reversed")
        if self.per_serving_mg is not None:
            if not fixed or self.daily_amount_mg_min != self.daily_amount_mg_max:
                raise ValueError("Serving arithmetic requires one fixed daily amount")
            if self.per_serving_mg * self.frequency != self.daily_amount_mg_min:
                raise ValueError("Serving amounts do not sum to the daily amount")
        return self


class ProtocolOption(FrozenReference):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    phases: tuple[ProtocolPhase, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_phases(self) -> "ProtocolOption":
        codes = [phase.code for phase in self.phases]
        if len(codes) != len(set(codes)):
            raise ValueError("Protocol phase codes must be unique")
        if any(phase.duration_days_min is None for phase in self.phases[:-1]):
            raise ValueError("Only the final protocol phase may be open-ended")
        return self


class ReviewedProtocol(FrozenReference):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    substance_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    population: str = Field(min_length=1, max_length=500)
    loading_optional: bool
    timing_statement: str = Field(min_length=1, max_length=1000)
    missed_dose_statement: str = Field(min_length=1, max_length=1000)
    options: tuple[ProtocolOption, ...] = Field(min_length=1, max_length=20)
    sources: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_options(self) -> "ReviewedProtocol":
        codes = [option.code for option in self.options]
        if len(codes) != len(set(codes)):
            raise ValueError("Protocol option codes must be unique")
        if any(not source.startswith("https://") for source in self.sources):
            raise ValueError("Protocol sources must be HTTPS URLs")
        return self


class ProtocolManifest(FrozenReference):
    manifest_version: str = Field(min_length=1, max_length=100)
    protocols: tuple[ReviewedProtocol, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_protocols(self) -> "ProtocolManifest":
        codes = [protocol.code for protocol in self.protocols]
        if len(codes) != len(set(codes)):
            raise ValueError("Protocol codes must be unique")
        return self


def load_protocol_manifest() -> ProtocolManifest:
    path = files("nutrition_bot.data.reference").joinpath("supplement_protocols.json")
    return ProtocolManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
