"""User-selected redistribution, never an estimate of exercise energy expenditure."""

from fractions import Fraction

KINDS = {"rest": 0, "gym": 1, "bjj": 1, "double": 2, "unknown": None}


def calorie_deltas(kinds: list[str], step_kcal: int) -> list[int]:
    if len(kinds) != 7 or any(kind not in KINDS for kind in kinds):
        raise ValueError("Supply seven days: rest, gym, bjj, double or unknown.")
    if type(step_kcal) is not int or not 0 <= step_kcal <= 300 or step_kcal % 4:
        raise ValueError("Choose a shift from 0 to 300 kcal in multiples of 4.")
    known = [index for index, kind in enumerate(kinds) if kind != "unknown"]
    result = [0] * 7
    if not known:
        return result
    weights = {index: int(KINDS[kinds[index]] or 0) for index in known}
    mean = Fraction(sum(weights.values()), len(known))
    exact = {index: (weight - mean) * (step_kcal // 4) for index, weight in weights.items()}
    units = {index: value.numerator // value.denominator for index, value in exact.items()}
    # Largest remainder with stable day-order tie breaks preserves the exact weekly sum.
    remainder = -sum(units.values())
    for index in sorted(known, key=lambda index: (-(exact[index] - units[index]), index))[
        :remainder
    ]:
        units[index] += 1
    for index, value in units.items():
        result[index] = value * 4
    assert sum(result) == 0
    return result
