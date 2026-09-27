"""Deterministic sums and scoped limits over recorded intake, never dose advice."""

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Literal


@dataclass(frozen=True)
class Exposure:
    day: date
    intake_id: int
    product_id: int | None
    substance: str
    name: str
    nutrient: str | None
    unit: str
    amount: int | None
    form: str | None
    comparison: str = "exact"


@dataclass(frozen=True)
class Amount:
    known: int | None
    known_entries: int
    entries: int
    missing_days: int = 0

    @property
    def partial(self) -> bool:
        return self.known is None or self.known_entries != self.entries or bool(self.missing_days)


def converted(amount: int | None, source: str, target: str) -> int | None:
    if amount is None:
        return None
    if source == target:
        return amount
    mass = {"g": 1_000_000, "mg": 1000, "ug": 1}
    if source not in mass or target not in mass:
        return None  # IU, DFE, RAE etc. need an explicitly reviewed conversion.
    value = Fraction(amount * mass[source], mass[target])
    return int(value) if value.denominator == 1 else None


def supplement_sum(rows: list[Exposure], unit: str) -> Amount:
    amounts = [
        converted(row.amount, row.unit, unit) if row.comparison == "exact" else None for row in rows
    ]
    known = [amount for amount in amounts if amount is not None]
    return Amount(sum(known) if known else (0 if not rows else None), len(known), len(rows))


def combine(food: Amount, supplement: Amount) -> Amount:
    values = [value for value in (food.known, supplement.known) if value is not None]
    # Empty supplement history contributes zero recorded doses, not knowledge of an
    # unlogged food day. Do not let its zero turn all-unknown food into known zero.
    meaningful = any(
        value.known is not None and (value.entries or not value.partial)
        for value in (food, supplement)
    )
    amount = sum(values) if values and meaningful else None
    if food.known is None and not supplement.entries:
        amount = None
    return Amount(
        amount,
        food.known_entries + supplement.known_entries,
        food.entries + supplement.entries,
        food.missing_days + supplement.missing_days,
    )


@dataclass(frozen=True)
class UpperLimit:
    nutrient: str
    unit: str
    amount: int
    scope: str
    form: str = ""


@dataclass(frozen=True)
class LimitResult:
    status: Literal["exceeds", "at_limit", "below_recorded", "indeterminate"]
    known: int | None
    incomplete: bool


def check_limit(
    limit: UpperLimit, food: Amount, rows: list[Exposure], *, food_complete: bool
) -> LimitResult:
    known: list[int] = []
    incomplete = False
    if limit.scope in {"food", "total", "fortified_and_supplement"}:
        # Food snapshots have no chemical-form or fortification fraction. Counting
        # all food toward a form/source-specific UL would overstate eligible exposure.
        if limit.form or limit.scope == "fortified_and_supplement":
            incomplete = food.entries > 0 or not food_complete
        else:
            if food.known is not None:
                known.append(food.known)
            incomplete = food.partial or not food_complete
    if limit.scope in {"supplement", "total", "fortified_and_supplement"}:
        for row in rows:
            if limit.form:
                if row.form is None or not row.form.strip():
                    incomplete = True
                    continue
                if row.form.strip().casefold() != limit.form.strip().casefold():
                    continue
            value = converted(row.amount, row.unit, limit.unit)
            if value is None or row.comparison in {"unknown", "less_than"}:
                incomplete = True
                continue
            if row.comparison == "at_least":
                incomplete = True
            known.append(value)
    total = sum(known) if known else (None if incomplete else 0)
    if total is not None and total > limit.amount:
        status: Literal["exceeds", "at_limit", "below_recorded", "indeterminate"] = "exceeds"
    elif incomplete:
        status = "indeterminate"
    elif total == limit.amount:
        status = "at_limit"
    else:
        status = "below_recorded"
    return LimitResult(status, total, incomplete)
