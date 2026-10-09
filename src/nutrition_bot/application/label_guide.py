"""Owner-reviewed label composition; model output and missing values have no authority."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
from nutrition_bot.adapters.database.schema import food_versions
from nutrition_bot.application.meal_conversation import MealReply, _short
from nutrition_bot.domain.food import (
    NutrientInput,
    ReviewedFoodInput,
    Unit,
    exact_decimal,
    grams_to_milligrams,
)

_UNITS: dict[str, Unit] = {
    "energy": "kcal",
    "protein": "g",
    "carbohydrate": "g",
    "fat": "g",
    "fiber": "g",
    "sodium": "mg",
    "potassium": "mg",
    "calcium": "mg",
    "magnesium": "mg",
    "iron": "mg",
    "zinc": "mg",
    "vitamin_c": "mg",
    "vitamin_d": "ug",
    "vitamin_b12": "ug",
}
_NAMES = {
    "energy": "Energy",
    "protein": "Protein",
    "carbohydrate": "Total carbohydrate including fiber",
    "fat": "Fat",
    "fiber": "Fiber",
    "sodium": "Sodium",
    "potassium": "Potassium",
    "calcium": "Calcium",
    "magnesium": "Magnesium",
    "iron": "Iron",
    "zinc": "Zinc",
    "vitamin_c": "Vitamin C",
    "vitamin_d": "Vitamin D",
    "vitamin_b12": "Vitamin B12",
}
_ALIASES = {name.casefold(): code for code, name in _NAMES.items()} | {
    "calories": "energy",
    "total fat": "fat",
    "fibre": "fiber",
    "dietary fiber": "fiber",
    "dietary fibre": "fiber",
    "vitamin b 12": "vitamin_b12",
    "total carbohydrate including fibre": "carbohydrate",
    "total carbohydrates including fiber": "carbohydrate",
    "total carbohydrates including fibre": "carbohydrate",
}
_AMBIGUOUS_CARBS = {
    "carbs",
    "carbohydrate",
    "carbohydrates",
    "total carbohydrate",
    "total carbohydrates",
}
_AVAILABLE_CARBS = {
    "available carbohydrate",
    "available carbohydrates",
    "carbohydrate excluding fiber",
    "carbohydrate excluding fibre",
}
_IGNORED = {
    "sugars",
    "sugar",
    "total sugars",
    "added sugars",
    "saturated fat",
    "saturates",
    "trans fat",
}
_HELP = (
    "Type the label's nutrient values for the selected serving basis, separated by lines "
    "or semicolons. E.g. Energy 120 kcal; Protein 10 g; Fat 4 g; "
    "Total carbohydrate including fiber 12 g; Sodium 100 mg. "
    "Include only reported values. Missing nutrients stay unknown."
)
_VALUE = re.compile(
    r"(.+?)(?:\s*[:=]\s*|\s+)([0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
    r"\s*(kcal|g|mg|ug|µg|μg|mcg|iu|kj)\s*",
    re.IGNORECASE,
)
_UNKNOWN = re.compile(r"(.+?)(?:\s*[:=]\s*|\s+)(?:unknown|not reported|missing)\s*", re.IGNORECASE)
_REFERENCE = "User-reviewed nutrition label: "
_INPUT_UNITS: dict[str, Unit] = {
    "kcal": "kcal",
    "g": "g",
    "mg": "mg",
    "ug": "ug",
    "µg": "ug",
    "μg": "ug",
    "mcg": "ug",
}


class LabelError(ValueError):
    """A bounded explanation safe to show beside a private label draft."""


@dataclass(frozen=True)
class ParsedLabel:
    nutrients: tuple[NutrientInput, ...]
    carbohydrate: NutrientInput | None
    warnings: tuple[str, ...]


def parse_label_values(text: str) -> ParsedLabel:
    """Parse only explicit nutrient definitions and units; never calculate nutrients."""
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 2000
        or any(unicodedata.category(c).startswith("C") and c not in "\n\r\t" for c in text)
    ):
        raise LabelError("Use up to 2000 characters of readable label values.")
    lines = [line.strip() for line in re.split(r"[;\n\r]+", text) if line.strip()]
    if not 1 <= len(lines) <= 30:
        raise LabelError("Enter one to thirty label values, separated by lines or semicolons.")
    values: dict[str, NutrientInput] = {}
    unknowns: dict[str, NutrientInput] = {}
    carbohydrate = None
    warnings = []
    for line in lines:
        matched = _VALUE.fullmatch(line)
        unknown = _UNKNOWN.fullmatch(line)
        if matched:
            key = " ".join(matched[1].rstrip(":=").casefold().split())
            amount = exact_decimal(matched[2])
            source_unit = matched[3].casefold()
            unit: Unit | None = _INPUT_UNITS.get(source_unit)
        elif unknown:
            key = " ".join(unknown[1].rstrip(":=").casefold().split())
            amount = None
            unit = None
            source_unit = ""
        else:
            raise LabelError(
                "Include a nutrient name, its reported amount and unit, e.g. "
                "Protein 10 g. Do not use a percent Daily Value as a mass."
            )
        if key in _IGNORED:
            warnings.append(f"{key.capitalize()} is not tracked by the current nutrient registry.")
            continue
        if key == "salt":
            if unit not in {"g", "mg", "ug", None}:
                raise LabelError("Salt requires a mass unit; it cannot be treated as sodium.")
            unknowns["sodium"] = NutrientInput(
                code="sodium",
                amount=None,
                unit="mg",
                note=f"Label reports salt {amount if amount is not None else 'unknown'} "
                f"{source_unit}; sodium was not supplied.",
            )
            warnings.append(
                "Salt was not converted to sodium; sodium stays unknown unless reported separately."
            )
            continue
        if key in _AVAILABLE_CARBS:
            if unit not in {"g", "mg", "ug", None}:
                raise LabelError("Carbohydrate requires a mass unit.")
            unknowns["carbohydrate"] = NutrientInput(
                code="carbohydrate",
                amount=None,
                unit="g",
                note=f"Label reports available carbohydrate "
                f"{amount if amount is not None else 'unknown'} {source_unit}; "
                "total carbohydrate including fiber is unknown.",
            )
            warnings.append(
                "Available carbohydrate was not mapped to total carbohydrate including fiber."
            )
            continue
        if key in _AMBIGUOUS_CARBS and amount is not None:
            if unit not in {"g", "mg", "ug"}:
                raise LabelError("Carbohydrate requires a mass unit.")
            if carbohydrate is not None or "carbohydrate" in values:
                raise LabelError("Enter only one carbohydrate value and definition.")
            assert unit is not None
            carbohydrate = NutrientInput(code="carbohydrate", amount=amount, unit=unit)
            continue
        code = "carbohydrate" if key in _AMBIGUOUS_CARBS else _ALIASES.get(key)
        if code is None:
            raise LabelError(
                "Use a supported nutrient name from the example or omit unsupported fields."
            )
        if code == "energy" and source_unit == "kj":
            unknowns[code] = NutrientInput(
                code=code,
                amount=None,
                unit="kcal",
                note=f"Label supplied energy {amount} kJ; kcal was not supplied.",
            )
            warnings.append(
                "Energy in kJ was not converted; supply the label's kcal value if available."
            )
            continue
        if code == "vitamin_d" and source_unit == "iu":
            unknowns[code] = NutrientInput(
                code=code,
                amount=None,
                unit="ug",
                note=f"Label supplied vitamin D {amount} IU; D2 plus D3 mass was not supplied.",
            )
            warnings.append("Vitamin D in IU was not converted; its mass stays unknown.")
            continue
        if amount is not None and (unit is None or (code == "energy") != (unit == "kcal")):
            raise LabelError(
                "Use kcal for energy and g, mg or ug for nutrient mass. "
                "The guide does not infer incompatible units."
            )
        if code in values or code == "carbohydrate" and carbohydrate is not None:
            raise LabelError("Enter each nutrient only once.")
        values[code] = NutrientInput(code=code, amount=amount, unit=unit or _UNITS[code])
    for code, value in unknowns.items():
        values.setdefault(code, value)
    return ParsedLabel(tuple(values.values()), carbohydrate, tuple(dict.fromkeys(warnings)))


def _digest(record: ReviewedFoodInput) -> str:
    return hashlib.sha256(
        json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _record(payload: dict[str, Any]) -> ReviewedFoodInput:
    return ReviewedFoodInput(
        name=payload["name"],
        preparation=payload["preparation"],
        basis_grams=payload["basis_grams"],
        source_reference=_REFERENCE + payload["name"],
        source_license="User-provided label transcription; no provider attribution.",
        nutrients=tuple(NutrientInput.model_validate(value) for value in payload["nutrients"]),
    )


async def _advance(
    connection: AsyncConnection,
    owner_id: int,
    stage: str,
    payload: dict[str, Any],
    text: str,
    actions: tuple[tuple[str, str], ...] = (),
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu

    revision = await begin(connection, owner_id, "label_" + stage, payload)
    return menu(
        text,
        tuple((name, f"flow:{revision}:label:{action}") for name, action in actions)
        + (("Cancel", "cancel"),),
    )


async def start(
    connection: AsyncConnection,
    owner_id: int,
    payload: dict[str, Any] | None = None,
    *,
    food_id: int | None = None,
    version_id: int | None = None,
) -> MealReply:
    state: dict[str, Any] = {"continuation_payload": dict(payload or {})}
    text = (
        "What is the packaged food's name? Include the brand or variant "
        "that identifies this label. "
    )
    if food_id is not None and version_id is not None:
        food = await get_food_version(connection, version_id)
        if food.food_id != food_id or not food.record.source_reference.startswith(_REFERENCE):
            raise LabelError("Choose a food saved through the label guide before correcting it.")
        state.update(food_id=food_id, expected_version_id=version_id)
        text += f"Current label: {food.record.name}. Corrections create a new food version. "
    return await _advance(
        connection,
        owner_id,
        "name",
        state,
        text + "No food consumption is logged by saving a label.",
    )


async def _values(connection: AsyncConnection, owner_id: int, payload: dict[str, Any]) -> MealReply:
    return await _advance(
        connection,
        owner_id,
        "nutrients",
        payload,
        f"{payload['name']} · values per {payload['basis_grams']} g\n" + _HELP,
    )


async def _preview(
    connection: AsyncConnection, owner_id: int, payload: dict[str, Any]
) -> MealReply:
    try:
        record = _record(payload)
    except ValidationError:
        raise LabelError(
            "At least one explicitly reported, supported nutrient value is needed. "
            "Enter another label value."
        ) from None
    state = {**payload, "record": record.model_dump(mode="json"), "record_hash": _digest(record)}
    by_code = {item.code: item for item in record.nutrients}
    lines = [
        f"Review label: {record.name}",
        f"Preparation: {record.preparation.replace('_', ' ')}",
        f"Label values per {record.basis_grams:f} g:",
    ]
    for code, name in _NAMES.items():
        value = by_code.get(code)
        lines.append(
            f"{name}: "
            + (
                f"{value.amount:f} {value.unit}"
                if value and value.amount is not None
                else "unknown"
            )
        )
    lines.extend(payload.get("warnings", []))
    lines.append(
        "Source: your reviewed nutrition label. Save food composition only; "
        "meal quantity and any estimate approval are separate."
    )
    return await _advance(
        connection,
        owner_id,
        "preview",
        state,
        "\n".join(lines),
        (
            ("Save food", "save"),
            ("Change nutrient values", "values"),
            ("Change serving basis", "basis"),
        ),
    )


async def guide_message(
    connection: AsyncConnection,
    row: Mapping[str, Any],
    text: str,
    message: Message,
    *,
    owner_id: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import menu

    payload = dict(row["payload"])
    if message.photo and not text:
        return menu(
            "This label step is still open. Type or paste the label's readable values "
            "with their units, using the selected serving basis. Keep the printed label "
            "beside the preview when reviewing it.",
            (("Cancel", "cancel"),),
        )
    try:
        if row["stage"] == "label_name":
            name = " ".join(unicodedata.normalize("NFC", text).split())
            if not 1 <= len(name) <= 120 or any(
                unicodedata.category(c).startswith("C") for c in text
            ):
                raise LabelError(
                    "Use a food name from 1 to 120 characters without control characters."
                )
            payload["name"] = name
            return await _advance(
                connection,
                owner_id,
                "preparation",
                payload,
                "Does this label describe the food as sold or after preparation?",
                (("As sold", "preparation=as_sold"), ("As prepared", "preparation=as_prepared")),
            )
        if row["stage"] == "label_basis":
            matched = re.fullmatch(r"([0-9]+(?:\.[0-9]{1,3})?)\s*(?:g|grams)?", text, re.IGNORECASE)
            if not matched:
                raise LabelError(
                    "Enter the label's serving mass in grams, e.g. 40 g. "
                    "Counts or volumes do not establish edible mass."
                )
            grams = exact_decimal(matched[1])
            if not 1 <= grams_to_milligrams(grams) <= 50_000_000:
                raise LabelError("Use a positive label basis no larger than 50000 g.")
            payload["basis_grams"] = format(grams, "f")
            return await _values(connection, owner_id, payload)
        if row["stage"] == "label_nutrients":
            parsed = parse_label_values(text)
            payload["nutrients"] = [value.model_dump(mode="json") for value in parsed.nutrients]
            payload["warnings"] = list(parsed.warnings)
            if parsed.carbohydrate is not None:
                payload["carbohydrate_pending"] = parsed.carbohydrate.model_dump(mode="json")
                return await _advance(
                    connection,
                    owner_id,
                    "carbohydrate",
                    payload,
                    "Does the carbohydrate value explicitly include fiber? The catalog stores "
                    "total carbohydrate including fiber. Available carbohydrate excluding "
                    "fiber cannot be treated as the same value.",
                    (
                        ("Total includes fiber", "carbohydrate=total"),
                        ("Available excludes fiber", "carbohydrate=available"),
                        ("Definition unknown", "carbohydrate=unknown"),
                    ),
                )
            return await _preview(connection, owner_id, payload)
        return menu(
            "Choose an action from the latest label buttons, or cancel.", (("Cancel", "cancel"),)
        )
    except (ValueError, ValidationError) as detail:
        error = (
            str(detail)
            if isinstance(detail, LabelError)
            else ("Check the nutrient definition, amount and units.")
        )
        return menu(
            error + "\nThis label step is still open; try again or cancel.", (("Cancel", "cancel"),)
        )


async def guide_action(
    connection: AsyncConnection,
    row: Mapping[str, Any],
    operation: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import menu

    payload = dict(row["payload"])
    operation = operation.removeprefix("label:")
    try:
        if operation.startswith("preparation=") and row["stage"] == "label_preparation":
            preparation = operation.split("=", 1)[1]
            if preparation not in {"as_sold", "as_prepared"}:
                raise LabelError("Choose the label's preparation.")
            payload["preparation"] = preparation
            return await _advance(
                connection,
                owner_id,
                "basis_choice",
                payload,
                "What amount do the printed nutrient values describe?",
                (("Per 100 g", "basis=100"), ("Per serving", "basis=serving")),
            )
        if operation in {"basis=100", "basis=serving"} and row["stage"] == "label_basis_choice":
            if operation == "basis=100":
                payload["basis_grams"] = "100"
                return await _values(connection, owner_id, payload)
            return await _advance(
                connection,
                owner_id,
                "basis",
                payload,
                "What is the printed serving's edible mass in grams? E.g. 40 g. "
                "Enter the serving basis, then its nutrient values; the amount eaten comes later.",
            )
        if operation.startswith("carbohydrate=") and row["stage"] == "label_carbohydrate":
            choice = operation.split("=", 1)[1]
            value = NutrientInput.model_validate(payload.pop("carbohydrate_pending"))
            if choice not in {"total", "available", "unknown"}:
                raise LabelError("Confirm the carbohydrate definition from the label.")
            if choice != "total":
                value = NutrientInput(
                    code="carbohydrate",
                    amount=None,
                    unit=value.unit,
                    note=f"Label carbohydrate {value.amount} {value.unit}: {choice}; "
                    "total including fiber unknown.",
                )
                payload["warnings"].append(
                    "Carbohydrate stays unknown because total including fiber was not confirmed."
                )
            payload["nutrients"] = [
                item for item in payload["nutrients"] if item["code"] != "carbohydrate"
            ] + [value.model_dump(mode="json")]
            try:
                return await _preview(connection, owner_id, payload)
            except LabelError:
                return await _values(connection, owner_id, payload)
        if operation == "values" and row["stage"] == "label_preview":
            return await _values(connection, owner_id, payload)
        if operation == "basis" and row["stage"] == "label_preview":
            return await _advance(
                connection,
                owner_id,
                "basis_choice",
                payload,
                "Choose the printed serving basis again. Re-enter nutrient values for that basis.",
                (("Per 100 g", "basis=100"), ("Per serving", "basis=serving")),
            )
        if operation == "save" and row["stage"] == "label_preview":
            record = ReviewedFoodInput.model_validate(payload["record"])
            if (
                _digest(record) != payload["record_hash"]
                or _digest(_record(payload)) != payload["record_hash"]
            ):
                raise LabelError(
                    "This label preview changed. Review the current values before saving."
                )
            if "food_id" in payload:
                latest = await connection.scalar(
                    sa.select(food_versions.c.id)
                    .where(
                        food_versions.c.food_id == payload["food_id"],
                        food_versions.c.sealed.is_(True),
                    )
                    .order_by(food_versions.c.version_number.desc())
                    .limit(1)
                )
                if latest != payload["expected_version_id"]:
                    raise LabelError(
                        "This food changed. Open its current label before correcting it."
                    )
            food = await publish_reviewed_food(connection, record, food_id=payload.get("food_id"))
            state = {
                "food_id": food.food_id,
                "version_id": food.version_id,
                "name": food.record.name,
                "continuation_payload": payload.get("continuation_payload", {}),
            }
            result = await _advance(
                connection,
                owner_id,
                "saved",
                state,
                f"Saved food: {food.record.name} "
                f"({food.record.preparation.replace('_', ' ')}).\n"
                "Reviewed label composition is available for future meals. No meal was logged.",
                (
                    (
                        "Continue meal" if state["continuation_payload"] else "Log this food",
                        "continue",
                    ),
                    ("Correct label", "correct"),
                ),
            )
            return result
        if operation == "correct" and row["stage"] == "label_saved":
            return await start(
                connection,
                owner_id,
                payload.get("continuation_payload"),
                food_id=payload["food_id"],
                version_id=payload["version_id"],
            )
        if operation == "continue" and row["stage"] == "label_saved":
            from nutrition_bot.application.food_discovery import select_food

            result = await select_food(
                connection,
                payload.get("continuation_payload") or {"continuation": "meal", "items": []},
                {"food_id": payload["version_id"], "name": payload["name"]},
                message,
                action_key=action_key,
                reference=reference,
                bot_id=bot_id,
                owner_id=owner_id,
                retention_days=retention_days,
                **context,
            )
            return replace(
                result, text="Label food saved; no meal was logged by saving it.\n" + result.text
            )
        return menu(
            "That label step changed. Use the latest label buttons.", (("Cancel", "cancel"),)
        )
    except (LabelError, ValueError, ValidationError) as error:
        text = (
            str(error)
            if isinstance(error, LabelError)
            else "This label record is invalid; check its nutrient units, "
            "serving basis and preparation."
        )
        return menu(
            _short(text, 1000) + "\nNothing was saved by this step.", (("Cancel", "cancel"),)
        )
