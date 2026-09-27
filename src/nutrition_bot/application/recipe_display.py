"""Recipe portions are separate from calculated ingredient equivalents."""

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from nutrition_bot.domain.recipe_portions import RecipeShare, ingredient_grams


def share_lines(shares: Iterable[RecipeShare | None]) -> list[str]:
    lines = []
    seen = set()
    for share in shares:
        if share is None:
            continue
        key = (
            share.version_id,
            share.unit,
            share.total_units,
            share.portion_units,
            share.yield_estimate_basis,
            share.portion_estimate_basis,
        )
        if key in seen:
            continue
        seen.add(key)
        amount = share.amount_text()
        if share.unit == "g":
            amount += " cooked"
        fraction = share.fraction
        lines.append(
            f"Recipe R{share.recipe_id}v{share.version_number}: {amount} · "
            f"{fraction.numerator}/{fraction.denominator} of batch"
            + (" · estimated portion" if share.portion_estimate_basis else "")
        )
        if share.yield_estimate_basis:
            lines.append("Batch yield/servings are estimated.")
    if lines:
        lines.append(
            "Ingredient equivalents below; nutrition calculated from ingredients. "
            "Cooking losses are not modeled."
        )
    return lines


def portion_mass(batch_milligrams: int, share: RecipeShare | None) -> str:
    if share is None:
        with localcontext() as context:
            context.prec = 50
            return f"{Decimal(batch_milligrams) / 1000:f} g"
    value = ingredient_grams(share, batch_milligrams)
    if value < Decimal("0.001"):
        return "<0.001 g equivalent"
    with localcontext() as context:
        context.prec = 50
        rounded = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN)
    return f"{rounded:f} g equivalent"
