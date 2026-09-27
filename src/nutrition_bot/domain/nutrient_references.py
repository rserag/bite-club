"""Versioned US/Canadian DRI subset matching the existing nutrient registry.

Numerical facts transcribed from Health Canada's DRI tables, checked 2026-09-27.
Not label Daily Values; no personal category or health suitability is inferred.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

VERSION = "adult-dri-2026-09-27-v1"
SOURCE = "https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables.html"
TABLE_ROOT = SOURCE.removesuffix(".html") + "/reference-values-"
SOURCES = {
    "elements": TABLE_ROOT + "elements.html",
    "vitamins": TABLE_ROOT + "vitamins.html",
    "macronutrients": TABLE_ROOT + "macronutrients.html",
}
SCOPE = (
    "Apparently healthy, non-pregnant, non-lactating adults; mixed diet, non-smokers. "
    "Reference categories are selected by you, never inferred. Clinical needs, "
    "vegetarian iron needs and different menstrual status require individual review."
)
GROUPS = tuple(
    f"{sex}-{age}" for sex in ("male", "female") for age in ("19-30", "31-50", "51-70", "71+")
)


@dataclass(frozen=True)
class ReferenceValue:
    reference_group: str
    nutrient_code: str
    kind: str
    amount_scaled: int
    unit: str
    applicability: str = "total"
    chemical_form: str = ""
    note: str = ""


def bundled_values() -> tuple[ReferenceValue, ...]:
    values = []
    for group in GROUPS:
        male = group.startswith("male-")
        age = group.split("-", 1)[1]
        over50 = age in {"51-70", "71+"}
        # Fixed unit-millionths avoid floating-point conversion of references.
        calcium = 1200 if age == "71+" or (not male and over50) else 1000
        magnesium = (400 if age == "19-30" else 420) if male else (310 if age == "19-30" else 320)
        entries = (
            (
                "fiber",
                "AI",
                (30 if over50 else 38) if male else (21 if over50 else 25),
                "g",
                "macronutrients",
            ),
            ("sodium", "AI", 1500, "mg", "elements"),
            ("sodium", "limit", 2300, "mg", "elements"),
            ("potassium", "AI", 3400 if male else 2600, "mg", "elements"),
            ("calcium", "RDA", calcium, "mg", "elements"),
            ("calcium", "UL", 2000 if over50 else 2500, "mg", "elements"),
            ("magnesium", "RDA", magnesium, "mg", "elements"),
            ("magnesium", "UL", 350, "mg", "elements"),
            ("iron", "RDA", 8 if male or over50 else 18, "mg", "elements"),
            ("iron", "UL", 45, "mg", "elements"),
            ("zinc", "RDA", 11 if male else 8, "mg", "elements"),
            ("zinc", "UL", 40, "mg", "elements"),
            ("vitamin_d", "RDA", 20 if age == "71+" else 15, "ug", "vitamins"),
            ("vitamin_d", "UL", 100, "ug", "vitamins"),
            ("vitamin_c", "RDA", 90 if male else 75, "mg", "vitamins"),
            ("vitamin_c", "UL", 2000, "mg", "vitamins"),
        )
        for code, kind, amount, unit, source in entries:
            scope = "supplement" if (code, kind) == ("magnesium", "UL") else "total"
            note = SOURCES[source]
            if code == "sodium" and kind == "limit":
                note += " | 2019 CDRR, not a UL or a minimum to fill."
            if code == "magnesium" and kind == "UL":
                note += " | Supplemental/pharmacological magnesium; excludes food and water."
            if code == "iron":
                note += " | Mixed diet; female 19-50 assumes menstruation, 51+ post-menopause."
            if code == "vitamin_d":
                note += " | Minimal sun exposure; no blood-status inference."
            if code == "vitamin_c":
                note += " | Non-smoker reference; smoking requires a different reference."
            values.append(
                ReferenceValue(group, code, kind, amount * 1_000_000, unit, scope, note=note)
            )
        values.append(
            ReferenceValue(
                group,
                "vitamin_b12",
                "RDA",
                2_400_000,
                "ug",
                note=SOURCES["vitamins"]
                + " | Over 50: absorption/source caveat; total intake does not establish status. "
                "No UL established.",
            )
        )
    return tuple(values)


def content_hash(values: tuple[ReferenceValue, ...]) -> str:
    payload = {"version": VERSION, "scope": SCOPE, "values": [asdict(v) for v in values]}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
