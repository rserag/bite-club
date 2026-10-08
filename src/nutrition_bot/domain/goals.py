from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CALCULATION_VERSION = "goal-targets-v1"


class GoalError(ValueError):
    pass


@dataclass(frozen=True)
class TargetProposal:
    method: str
    mode: str
    target_rate_grams_per_week: int
    reference_weight_grams: int
    inputs: dict[str, int | str]
    estimated_tdee_kcal: int | None
    energy_kcal: int
    protein_grams: int
    fat_grams: int
    carbohydrate_grams: int
    energy_range_low_kcal: int | None
    energy_range_high_kcal: int | None


def _round(value: Decimal, increment: int) -> int:
    units = (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(units) * increment


def _validate_goal(mode: str, weight_kg: Decimal, rate_kg: Decimal) -> int:
    if mode not in {"loss", "maintenance", "gain"}:
        raise GoalError("Goal must be loss, maintenance, or gain.")
    if not Decimal("30") <= weight_kg <= Decimal("300"):
        raise GoalError("Reference weight must be between 30 and 300 kg.")
    if mode == "maintenance":
        if rate_kg != 0:
            raise GoalError("Maintenance uses rate 0 kg/week.")
        return 0
    fraction = rate_kg / weight_kg
    lower, upper = (
        (Decimal("0.0025"), Decimal("0.005"))
        if mode == "loss"
        else (Decimal("0.001"), Decimal("0.0025"))
    )
    if not lower <= fraction <= upper:
        label = "0.25–0.5%" if mode == "loss" else "0.1–0.25%"
        raise GoalError(f"Choose a conservative {mode} rate of {label} of body weight per week.")
    grams = _round(rate_kg * 1000, 10)
    return -grams if mode == "loss" else grams


def _macros(mode: str, weight_kg: Decimal, energy_kcal: int) -> tuple[int, int, int]:
    protein = _round(weight_kg * (Decimal("2.0") if mode == "loss" else Decimal("1.8")), 5)
    fat = _round(weight_kg * Decimal("0.8"), 5)
    fat_share = Decimal(fat * 9) / energy_kcal
    if fat_share > Decimal("0.35"):
        raise GoalError(
            "Those calories are too low for the default fat target. Use manual targets."
        )
    carbohydrate_energy = energy_kcal - protein * 4 - fat * 9
    if carbohydrate_energy < 0:
        raise GoalError("Those calories cannot support the protein and fat targets.")
    carbohydrate = _round(Decimal(carbohydrate_energy) / 4, 5)
    return protein, fat, carbohydrate


def estimate_targets(
    *,
    mode: str,
    weight_kg: Decimal,
    height_cm: Decimal,
    age_years: int,
    rmr_coefficient: int,
    activity_factor: Decimal,
    rate_kg_per_week: Decimal,
) -> TargetProposal:
    rate = _validate_goal(mode, weight_kg, rate_kg_per_week)
    if not Decimal("120") <= height_cm <= Decimal("230"):
        raise GoalError("Height must be between 120 and 230 cm.")
    if not 18 <= age_years <= 100:
        raise GoalError("Age must be between 18 and 100 years.")
    if rmr_coefficient not in {5, -161}:
        raise GoalError("Choose the Mifflin–St Jeor coefficient +5 or -161 explicitly.")
    if activity_factor not in {Decimal("1.4"), Decimal("1.6"), Decimal("1.8")}:
        raise GoalError("Activity must be 1.4, 1.6, or 1.8.")
    rmr = weight_kg * 10 + height_cm * Decimal("6.25") - age_years * 5 + rmr_coefficient
    tdee = _round(rmr * activity_factor, 50)
    requested = Decimal(tdee) + Decimal(rate) * Decimal("7.7") / 7
    if mode == "loss":
        requested = max(requested, Decimal(tdee) * Decimal("0.8"))
    elif mode == "gain":
        requested = min(requested, Decimal(tdee) * Decimal("1.1"))
    energy = _round(requested, 50)
    protein, fat, carbohydrate = _macros(mode, weight_kg, energy)
    if not (
        1 <= energy <= 10000
        and 20 <= protein <= 500
        and 20 <= fat <= 300
        and 0 <= carbohydrate <= 1200
    ):
        raise GoalError("This estimate exceeds the supported plan range. Use /goal manual.")
    weight_grams = _round(weight_kg * 1000, 1)
    return TargetProposal(
        method="estimate",
        mode=mode,
        target_rate_grams_per_week=rate,
        reference_weight_grams=weight_grams,
        inputs={
            "height_cm": str(height_cm),
            "age_years": age_years,
            "rmr_coefficient": rmr_coefficient,
            "activity_factor": str(activity_factor),
        },
        estimated_tdee_kcal=tdee,
        energy_kcal=energy,
        protein_grams=protein,
        fat_grams=fat,
        carbohydrate_grams=carbohydrate,
        energy_range_low_kcal=max(1, energy - 100),
        energy_range_high_kcal=min(10000, energy + 100),
    )


def manual_targets(
    *,
    mode: str,
    weight_kg: Decimal,
    rate_kg_per_week: Decimal,
    energy_kcal: int,
    protein_grams: int,
    fat_grams: int,
) -> TargetProposal:
    rate = _validate_goal(mode, weight_kg, rate_kg_per_week)
    if not 1 <= energy_kcal <= 10000:
        raise GoalError("Calories must be a positive value no greater than 10000 kcal.")
    if not 20 <= protein_grams <= 500 or not 20 <= fat_grams <= 300:
        raise GoalError("Protein must be 20–500 g and fat 20–300 g.")
    remaining = energy_kcal - protein_grams * 4 - fat_grams * 9
    if remaining < 0:
        raise GoalError("Calories are lower than the supplied protein and fat require.")
    carbohydrate = _round(Decimal(remaining) / 4, 5)
    if not 0 <= carbohydrate <= 1200:
        raise GoalError("Carbohydrate must be 0–1200 g. Lower calories or adjust protein and fat.")
    return TargetProposal(
        method="manual",
        mode=mode,
        target_rate_grams_per_week=rate,
        reference_weight_grams=_round(weight_kg * 1000, 1),
        inputs={},
        estimated_tdee_kcal=None,
        energy_kcal=energy_kcal,
        protein_grams=protein_grams,
        fat_grams=fat_grams,
        carbohydrate_grams=carbohydrate,
        energy_range_low_kcal=None,
        energy_range_high_kcal=None,
    )
