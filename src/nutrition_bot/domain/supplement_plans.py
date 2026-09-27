"""Reviewed, frozen calendar plans; no adherence-driven dose changes."""

from datetime import date, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from nutrition_bot.domain.supplements import FrozenReference, load_protocol_manifest


class PlanPhase(FrozenReference):
    name: str
    offset: int = Field(ge=0)
    days: int | None = Field(default=None, ge=1)
    dose_mg: int = Field(gt=0, le=100_000)
    frequency: int = Field(ge=1, le=24)


class CreatinePlan(FrozenReference):
    start: date
    timezone: str
    option: Literal["steady", "fixed_loading", "weight_loading"]
    maintenance_mg: int = Field(ge=3000, le=5000)
    loading_days: int = Field(default=0, ge=0, le=7)
    weight_grams: int | None = Field(default=None, ge=30000, le=300000)
    protocol_version: str

    @model_validator(mode="after")
    def validate_plan(self) -> "CreatinePlan":
        ZoneInfo(self.timezone)
        if self.option == "steady":
            if self.loading_days or self.weight_grams is not None:
                raise ValueError("Steady plans have no loading days or weight snapshot")
        elif not 5 <= self.loading_days <= 7:
            raise ValueError("Choose 5–7 loading days")
        if (self.option == "weight_loading") != (self.weight_grams is not None):
            raise ValueError("Only weight-based loading requires an explicit starting weight")
        return self

    def phases(self) -> tuple[PlanPhase, ...]:
        maintenance = PlanPhase(
            name="maintenance", offset=self.loading_days, dose_mg=self.maintenance_mg, frequency=1
        )
        if self.option == "steady":
            return (maintenance,)
        # Round each of four equal portions to the nearest milligram; preview discloses this.
        dose = 5000 if self.weight_grams is None else (self.weight_grams * 3 + 20) // 40
        return (
            PlanPhase(name="loading", offset=0, days=self.loading_days, dose_mg=dose, frequency=4),
            maintenance,
        )

    def phase_on(self, day: date) -> tuple[int, PlanPhase] | None:
        offset = (day - self.start).days
        for index, phase in enumerate(self.phases(), 1):
            if offset >= phase.offset and (
                phase.days is None or offset < phase.offset + phase.days
            ):
                return index, phase
        return None

    def preview(self) -> str:
        lines = [
            f"Creatine monohydrate · {self.timezone}",
            "Measured grams; no product or scoop inferred.",
        ]
        for phase in self.phases():
            start = self.start + timedelta(days=phase.offset)
            end = "ongoing" if phase.days is None else str(start + timedelta(days=phase.days - 1))
            lines.append(
                f"{phase.name}: {start} → {end}; {phase.frequency} × "
                f"{phase.dose_mg / 1000:g} g = {phase.frequency * phase.dose_mg / 1000:g} g/day"
            )
        if self.weight_grams is not None:
            lines.append(
                f"Frozen starting weight: {self.weight_grams / 1000:g} kg; "
                "0.3 g/kg/day, each of four portions rounded to 1 mg."
            )
        lines.append(
            f"Reference version: {self.protocol_version}. Loading is optional. "
            "Unlogged doses stay unconfirmed; no catch-up or phase extension."
        )
        return "\n".join(lines)


def protocol_version() -> str:
    return load_protocol_manifest().manifest_version
