"""Bounded USDA FoodData Central access and explicit nutrient interpretation.

The mapping is for full generic-food details, not search snippets or branded labels.
Source values are per 100 g edible food; USDA portion weights remain estimates.
"""

import asyncio
import json
import math
import re
from decimal import Decimal
from typing import cast

import httpx
from pydantic import SecretStr, ValidationError

from nutrition_bot.domain.food import (
    NutrientInput,
    PortionInput,
    Preparation,
    ReviewedFoodInput,
    Unit,
    exact_decimal,
    grams_to_milligrams,
)
from nutrition_bot.domain.food_source import FoodCandidate, ProviderError, SourceDocument

BASE_URL = "https://api.nal.usda.gov/fdc/v1"
ADAPTER_VERSION = "usda-generic-v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SEARCH_PAGE_SIZE = 10
DATA_TYPES = ("Foundation", "SR Legacy", "Survey (FNDDS)")
ENERGY_PRIORITY = (2048, 2047, 1008)
# These are nutrient IDs, not the legacy nutrient.number used by API filters.
NUTRIENT_MAP: dict[int, tuple[str, Unit]] = {
    2048: ("energy", "kcal"),
    2047: ("energy", "kcal"),
    1008: ("energy", "kcal"),
    1003: ("protein", "g"),
    1004: ("fat", "g"),
    1005: ("carbohydrate", "g"),
    1079: ("fiber", "g"),
    1093: ("sodium", "mg"),
    1092: ("potassium", "mg"),
    1087: ("calcium", "mg"),
    1090: ("magnesium", "mg"),
    1089: ("iron", "mg"),
    1095: ("zinc", "mg"),
    1114: ("vitamin_d", "ug"),
    1178: ("vitamin_b12", "ug"),
    1162: ("vitamin_c", "mg"),
}


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected object")
    return cast(dict[str, object], value)


def _text(value: object, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError("Invalid text")
    return value.strip()


def _optional_text(value: object, maximum: int = 500) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, maximum)


def _source_id(value: object) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", value):
        raise ValueError("Invalid USDA food id")
    return value


def _preparation(name: str) -> Preparation:
    tokens = set(re.findall(r"[a-z]+", name.casefold()))
    raw = bool(tokens & {"raw", "uncooked"})
    cooked = bool(tokens & {"cooked", "boiled", "baked", "roasted", "fried", "grilled", "steamed"})
    if raw == cooked or (cooked and tokens & {"not", "unheated"}):
        return "unspecified"
    return "raw" if raw else "cooked"


def _candidate(value: object) -> FoodCandidate:
    item = _object(value)
    data_type = _text(item.get("dataType"), 50)
    if data_type not in DATA_TYPES:
        raise ProviderError("unsupported_food")
    name = _text(item.get("description"))
    return FoodCandidate(
        source_id=_source_id(item.get("fdcId")),
        name=name,
        data_type=data_type,
        preparation_hint=_preparation(name),
        brand=_optional_text(item.get("brandOwner")),
    )


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _retry_after(value: str | None) -> int | None:
    if value is not None and re.fullmatch(r"[0-9]{1,6}", value):
        return min(int(value), 86400)
    return None


class USDAProvider:
    def __init__(
        self, client: httpx.AsyncClient, api_key: SecretStr | None, timeout_seconds: float = 10
    ):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("USDA timeout must be between zero and 60 seconds")
        self._client = client
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def _request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        if self._api_key is None or not self._api_key.get_secret_value().strip():
            raise ProviderError("not_configured")
        try:
            # The outer deadline also bounds slow streams that keep resetting read timeouts.
            async with asyncio.timeout(self._timeout_seconds):
                async with self._client.stream(
                    method,
                    BASE_URL + path,
                    json=body,
                    headers={
                        "X-Api-Key": self._api_key.get_secret_value(),
                        "Accept": "application/json",
                        # Keep the memory bound effective before HTTPX decodes a chunk.
                        "Accept-Encoding": "identity",
                    },
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 429:
                        raise ProviderError(
                            "rate_limited", _retry_after(response.headers.get("Retry-After"))
                        )
                    if response.status_code == 404:
                        raise ProviderError("not_found")
                    if response.status_code != 200:
                        raise ProviderError("unavailable")
                    if (
                        response.headers.get("Content-Encoding", "identity").casefold()
                        != "identity"
                    ):
                        raise ProviderError("invalid_response")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProviderError("invalid_response")
                        payload.extend(chunk)
                value = json.loads(
                    payload,
                    parse_float=Decimal,
                    parse_constant=_reject_constant,
                )
                return _object(value)
        except TimeoutError:
            raise ProviderError("timeout") from None
        except httpx.TimeoutException:
            raise ProviderError("timeout") from None
        except httpx.DecodingError:
            raise ProviderError("invalid_response") from None
        except httpx.HTTPError:
            raise ProviderError("unavailable") from None
        except (ValueError, RecursionError):
            raise ProviderError("invalid_response") from None

    async def search(self, query: str) -> tuple[FoodCandidate, ...]:
        query = _text(query, 200)
        response = await self._request(
            "POST",
            "/foods/search",
            {
                "query": query,
                "dataType": list(DATA_TYPES),
                "pageSize": SEARCH_PAGE_SIZE,
                "pageNumber": 1,
            },
        )
        items = response.get("foods")
        if not isinstance(items, list) or len(items) > 200:
            raise ProviderError("invalid_response")
        result: list[FoodCandidate] = []
        seen: set[str] = set()
        for item in items:
            try:
                candidate = _candidate(item)
            except (ValueError, ProviderError):
                continue
            if candidate.source_id in seen:
                continue
            result.append(candidate)
            seen.add(candidate.source_id)
            if len(result) == SEARCH_PAGE_SIZE:
                break
        return tuple(result)

    async def fetch(self, source_id: str) -> SourceDocument:
        source_id = _source_id(source_id)
        response = await self._request("GET", f"/food/{source_id}?format=full")
        try:
            candidate = _candidate(response)
            if candidate.source_id != source_id:
                raise ValueError("Unexpected food id")
            warnings: list[str] = []
            nutrients = _nutrients(response.get("foodNutrients"), warnings)
            portions = _portions(response.get("foodPortions", []), candidate, warnings)
            record = ReviewedFoodInput(
                name=candidate.name,
                preparation=candidate.preparation_hint,
                brand=candidate.brand,
                source_reference=f"USDA FoodData Central FDC {source_id}; {candidate.data_type}",
                source_url=f"https://fdc.nal.usda.gov/food-details/{source_id}/nutrients",
                source_license="CC0-1.0",
                basis_grams=Decimal(100),
                nutrients=nutrients,
                portions=portions,
            )
            return SourceDocument(
                source_id=source_id,
                record=record,
                data_type=candidate.data_type,
                source_published_date=_optional_text(response.get("publicationDate"), 50),
                adapter_version=ADAPTER_VERSION,
                warnings=tuple(sorted(set(warnings))),
            )
        except (ValueError, RecursionError):
            raise ProviderError("invalid_response") from None


def _nutrients(value: object, warnings: list[str]) -> tuple[NutrientInput, ...]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("Invalid nutrients")
    by_id: dict[int, list[dict[str, object]]] = {}
    for raw in value:
        item = _object(raw)
        nutrient = _object(item.get("nutrient"))
        nutrient_id = nutrient.get("id")
        if type(nutrient_id) is not int:
            raise ValueError("Invalid nutrient id")
        if nutrient_id not in NUTRIENT_MAP:
            warnings.append("unsupported_nutrients_omitted")
            continue
        by_id.setdefault(nutrient_id, []).append(item)
    energy_id = next((item for item in ENERGY_PRIORITY if item in by_id), None)
    result: list[NutrientInput] = []
    for nutrient_id, items in by_id.items():
        if nutrient_id in ENERGY_PRIORITY and nutrient_id != energy_id:
            continue
        code, unit = NUTRIENT_MAP[nutrient_id]
        if len(items) != 1:
            result.append(
                NutrientInput(
                    code=code,
                    unit=unit,
                    amount=None,
                    note=f"USDA nutrient {nutrient_id}; duplicate source values; unknown",
                )
            )
            warnings.append("duplicate_nutrient_unknown")
            continue
        result.append(_nutrient_value(nutrient_id, items[0], warnings))
    if not result:
        raise ProviderError("unsupported_food")
    return tuple(sorted(result, key=lambda item: item.code))


def _nutrient_value(
    nutrient_id: int, item: dict[str, object], warnings: list[str]
) -> NutrientInput:
    code, unit = NUTRIENT_MAP[nutrient_id]
    nutrient = _object(item["nutrient"])
    supplied_unit = _optional_text(nutrient.get("unitName"), 20)
    normalized_unit = (supplied_unit or "").casefold().replace("µ", "u").replace("μ", "u")
    notes = [f"USDA nutrient {nutrient_id}"]
    if nutrient_id in ENERGY_PRIORITY:
        notes.append("energy preference: 2048, 2047, 1008; never summed")
    derivation = item.get("foodNutrientDerivation")
    if isinstance(derivation, dict):
        derivation_code = _optional_text(derivation.get("code"), 30)
        if derivation_code:
            notes.append(f"derivation {derivation_code}")
    amount: Decimal | None = None
    if normalized_unit != unit:
        notes.append("missing or incompatible unit; unknown")
        warnings.append("nutrient_unit_unknown")
    elif item.get("loq") is not None:
        try:
            loq = exact_decimal(item["loq"])
        except ValueError:
            notes.append("invalid quantification limit; unknown")
        else:
            notes.append(f"quantification limit {loq} {unit}; amount treated as unknown")
        warnings.append("quantification_limit_unknown")
    elif item.get("amount") is None:
        notes.append("amount missing; unknown")
    else:
        try:
            amount = exact_decimal(item["amount"])
        except ValueError:
            notes.append("invalid amount; unknown")
            warnings.append("nutrient_amount_unknown")
    return NutrientInput(code=code, unit=unit, amount=amount, note="; ".join(notes))


def _portion_label(item: dict[str, object], data_type: str) -> str:
    description = _optional_text(item.get("portionDescription"))
    if data_type == "Survey (FNDDS)":
        # FNDDS amount is embedded in this description; modifier is a code, not a unit.
        if description is None:
            raise ValueError("Missing survey portion description")
        return description
    amount = exact_decimal(item.get("amount"))
    if amount <= 0:
        raise ValueError("Invalid portion amount")
    modifier = _optional_text(item.get("modifier"))
    if data_type == "SR Legacy":
        # SR puts its unit and qualifiers in modifier; measureUnit is undetermined.
        if modifier is None:
            raise ValueError("Missing legacy measure")
        return f"{amount:f} {modifier}"
    measure = _object(item.get("measureUnit"))
    unit = _text(measure.get("name"), 100)
    if unit.casefold() in {"undetermined", "unknown", "not applicable"}:
        raise ValueError("Unknown measure")
    label = f"{amount:f} {unit}"
    if modifier:
        label += f", {modifier}"
    if description:
        label += f" ({description})"
    return label


def _portions(
    value: object, candidate: FoodCandidate, warnings: list[str]
) -> tuple[PortionInput, ...]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("Invalid portions")
    result: list[PortionInput] = []
    seen: set[str] = set()
    for raw in value:
        try:
            item = _object(raw)
            label = _portion_label(item, candidate.data_type)
            portion_id = item.get("id")
            source = f"USDA FDC {candidate.source_id}; published average edible weight"
            if type(portion_id) is int:
                source += f"; portion {portion_id}"
            portion = PortionInput(
                label=label,
                original_measure=label,
                # gramWeight is the full described amount, not grams per unit.
                grams=exact_decimal(item.get("gramWeight")),
                source=source,
                is_estimate=True,
            )
            grams_to_milligrams(portion.grams)
        except (ValueError, ValidationError):
            warnings.append("invalid_portion_omitted")
            continue
        if label.casefold() in seen:
            warnings.append("duplicate_portion_omitted")
            continue
        if len(result) == 100:
            warnings.append("excess_portions_omitted")
            break
        result.append(portion)
        seen.add(label.casefold())
    return tuple(result)
