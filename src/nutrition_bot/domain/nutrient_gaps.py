"""Conservative repeated intake screening; no deficiency or adequacy diagnosis."""

from dataclasses import dataclass
from datetime import date

from nutrition_bot.domain.supplement_reports import Exposure, supplement_sum


@dataclass(frozen=True)
class FoodValue:
    day: date
    name: str
    amount: int | None
    quality: str
    estimated: bool = False


@dataclass(frozen=True)
class Week:
    complete_days: int
    observed: int | None
    imputed: int
    supplement: int | None
    known_entries: int
    entries: int
    unknown_foods: tuple[str, ...]
    uncertain_supplements: bool
    estimated_entries: int

    @property
    def eligible(self) -> bool:
        # Even >=90% is insufficient when an unknown food might be material.
        # No materiality bound exists in this ledger, so all such foods block.
        return (
            self.complete_days >= 5
            and self.known_entries * 10 >= self.entries * 9
            and not self.unknown_foods
            and not self.uncertain_supplements
            and self.observed is not None
        )


def summarize(complete: set[date], food: list[FoodValue], doses: list[Exposure], unit: str) -> Week:
    rows = [r for r in food if r.day in complete]
    observed = [
        r.amount
        for r in rows
        if r.amount is not None and r.quality in {"manual_reviewed", "source_reported"}
    ]
    unknown = tuple(
        sorted(
            {
                r.name
                for r in rows
                if r.amount is None or r.quality not in {"manual_reviewed", "source_reported"}
            }
        )
    )
    imputed = sum(
        r.amount or 0 for r in rows if r.quality not in {"manual_reviewed", "source_reported"}
    )
    supp = supplement_sum([r for r in doses if r.day in complete], unit)
    return Week(
        len(complete),
        sum(observed) if observed or (complete and not rows) else None,
        imputed,
        supp.known,
        len(observed),
        len(rows),
        unknown,
        supp.partial,
        sum(r.estimated for r in rows),
    )


def screen(
    weeks: tuple[Week, Week], *, code: str, kind: str, target: int, scope: str, form: str = ""
) -> str:
    if code == "sodium" or kind in {"AI", "limit", "UL"}:
        return "not_gap_reference"
    if kind != "RDA" or scope not in {"food", "total"} or form:
        return "unsupported_reference"
    if any(w.complete_days < 5 for w in weeks):
        return "insufficient_days"
    if not all(w.eligible for w in weeks):
        return "coverage_uncertain"
    # Compare rational means exactly; no rounding at the 80% boundary.
    if all(
        ((w.observed or 0) + ((w.supplement or 0) if scope == "total" else 0)) * 5
        < target * w.complete_days * 4
        for w in weeks
    ):
        return "possible_intake_gap"
    return "no_repeated_trigger"
