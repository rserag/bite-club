"""Read-only day summaries of recorded food, not estimates of complete daily intake."""

from dataclasses import dataclass
from datetime import date

from nutrition_bot.domain.food import Unit

CORE_NUTRIENT_ORDER = (
    "energy",
    "protein",
    "carbohydrate",
    "fat",
    "fiber",
    "sodium",
    "potassium",
    "calcium",
    "magnesium",
    "iron",
    "zinc",
    "vitamin_d",
    "vitamin_b12",
    "vitamin_c",
)


@dataclass(frozen=True, slots=True)
class DailyMeal:
    id: int
    revision_number: int
    label: str


@dataclass(frozen=True, slots=True)
class DailyNutrient:
    code: str
    name: str
    unit: Unit
    # Consumed millionths of the canonical unit; None means no known values.
    known_amount_scaled: int | None
    known_items: int
    total_items: int
    quality_counts: tuple[tuple[str, int], ...]

    @property
    def missing_items(self) -> int:
        return self.total_items - self.known_items


@dataclass(frozen=True, slots=True)
class DailyTotals:
    local_date: date
    meals: tuple[DailyMeal, ...]
    item_count: int
    nutrients: tuple[DailyNutrient, ...]
    source_counts: tuple[tuple[str, int], ...]
    quantity_counts: tuple[tuple[str, int], ...]
    recorded_timezones: tuple[str, ...]
