"""Read-only barcode lookup with reported label values and bounded failures.

No photos or user text are uploaded. Only validated digits reach the fixed
provider endpoint. Original label amounts/units determine the nutrient basis;
computed salt/sodium, energy and ambiguous carbohydrate values are not inferred.
"""

import asyncio
import json
import math
import time
from decimal import Decimal
from typing import cast

import httpx
from pydantic import ValidationError

from nutrition_bot.domain.barcodes import normalize_barcode
from nutrition_bot.domain.food import NutrientInput, ReviewedFoodInput, Unit, exact_decimal
from nutrition_bot.domain.food_source import FoodCandidate, ProviderError, SourceDocument

BASE_URL = "https://world.openfoodfacts.org/api/v3.6/product/"
ADAPTER_VERSION = "off-reported-label-v1"
USER_AGENT = "BiteClub/0.1.0 (https://github.com/rserag/bite-club)"
MAX_RESPONSE_BYTES = 512 * 1024
REQUEST_INTERVAL_SECONDS = 4.1  # below the provider's 15 product reads/minute
FIELDS = "code,product_name,brands,nutrition,last_modified_t"
_NUTRIENTS: tuple[tuple[str, str, Unit], ...] = (
    ("energy-kcal", "energy", "kcal"),
    ("proteins", "protein", "g"),
    ("carbohydrates", "carbohydrate", "g"),
    ("fat", "fat", "g"),
    ("fiber", "fiber", "g"),
    ("sodium", "sodium", "mg"),
    ("potassium", "potassium", "mg"),
    ("calcium", "calcium", "mg"),
    ("magnesium", "magnesium", "mg"),
    ("iron", "iron", "mg"),
    ("zinc", "zinc", "mg"),
    ("vitamin-d", "vitamin_d", "ug"),
    ("vitamin-b12", "vitamin_b12", "ug"),
    ("vitamin-c", "vitamin_c", "mg"),
)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected product object")
    return cast(dict[str, object], value)


def _text(value: object, maximum: int = 500) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError("Invalid source text")
    return value.strip()


def _unit(value: object) -> Unit | None:
    if not isinstance(value, str):
        return None
    units: dict[str, Unit] = {"kcal": "kcal", "g": "g", "mg": "mg", "ug": "ug"}
    return units.get(value.strip().casefold().replace("µ", "u").replace("μ", "u"))


def _record(product: dict[str, object], code: str) -> tuple[ReviewedFoodInput, tuple[str, ...]]:
    name = _text(product.get("product_name"))
    nutrition = _object(product.get("nutrition"))
    input_sets = nutrition.get("input_sets")
    if not isinstance(input_sets, list):
        raise ProviderError("unsupported_food")
    eligible = [
        _object(item)
        for item in input_sets
        if isinstance(item, dict)
        and item.get("per") == "100g"
        and item.get("preparation") == "as_sold"
        and item.get("source") in {"packaging", "manufacturer"}
        and item.get("per_quantity") in (None, 100)
        and item.get("per_unit") in (None, "g")
    ]
    # Prefer the printed packaging set, then manufacturer input. Never merge
    # sets, infer grams from volume, or import the computed aggregated_set.
    packaging = [item for item in eligible if item.get("source") == "packaging"]
    selected = packaging or eligible
    if len(selected) != 1:
        raise ProviderError("unsupported_food")
    values = _object(selected[0].get("nutrients"))
    warnings = [
        "Community-reported product data: check the product and printed label.",
        f"Original as-sold 100 g input set: {selected[0]['source']}.",
    ]
    nutrients: list[NutrientInput] = []
    for source, target, canonical in _NUTRIENTS:
        amount = None
        unit: Unit = canonical
        reason: str | None = None
        raw = values.get(source)
        nutrient = _object(raw) if isinstance(raw, dict) else {}
        # value is normalized and value_computed can be inferred. Only the
        # original value_string and contributor unit establish label amounts.
        entered = nutrient.get("value_string")
        source_unit = _unit(nutrient.get("unit"))
        if entered is not None:
            if nutrient.get("modifier") not in (None, "", "="):
                reason = "The source amount is qualified; kept unknown."
            elif source_unit is None or (canonical == "kcal") != (source_unit == "kcal"):
                reason = "The original source unit is unavailable or incompatible."
            elif target == "carbohydrate" and str(
                nutrient.get("label", "")
            ).strip().casefold() not in {"total carbohydrate", "total carbohydrates"}:
                reason = "Total carbohydrate including fiber is not established; kept unknown."
            else:
                try:
                    amount = exact_decimal(entered)
                    unit = source_unit
                except ValueError:
                    reason = "The source amount is unusable; kept unknown."
        if reason:
            warnings.append(f"{target}: {reason}")
        nutrients.append(
            NutrientInput(
                code=target,
                amount=amount,
                unit=unit,
                note=reason
                or (
                    "Original value and unit reported for an as-sold 100 g label basis."
                    if amount is not None
                    else "No usable original label value; missing is not zero."
                ),
            )
        )
    brand = product.get("brands")
    record = ReviewedFoodInput(
        name=name,
        brand=None if brand in (None, "") else _text(brand),
        preparation="as_sold",
        source_reference=f"Open Food Facts barcode {code}; community-reported product label",
        source_url=f"https://world.openfoodfacts.org/product/{code}",
        source_license="ODbL-1.0 database; DbCL-1.0 contents; attribution: Open Food Facts",
        basis_grams=Decimal(100),
        nutrients=tuple(nutrients),
    )
    return record, tuple(warnings)


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite source number")


class OpenFoodFactsProvider:
    def __init__(self, client: httpx.AsyncClient, timeout_seconds: float = 10):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Product lookup timeout must be between zero and 30 seconds")
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()
        self._last_request = -float("inf")

    async def search(self, query: str) -> tuple[FoodCandidate, ...]:
        document = await self.fetch(query)
        return (
            FoodCandidate(
                source_id=document.source_id,
                name=document.record.name,
                data_type=document.data_type,
                preparation_hint="as_sold",
                brand=document.record.brand,
                provider="openfoodfacts",
            ),
        )

    async def fetch(self, source_id: str) -> SourceDocument:
        try:
            code = normalize_barcode(source_id)
        except ValueError:
            raise ProviderError("invalid_response") from None
        try:
            async with asyncio.timeout(self._timeout_seconds), self._lock:
                delay = REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request = time.monotonic()
                async with self._client.stream(
                    "GET",
                    BASE_URL + code + ".json",
                    params={"fields": FIELDS},
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    follow_redirects=False,
                    timeout=self._timeout_seconds,
                ) as response:
                    if response.status_code == 404:
                        raise ProviderError("not_found")
                    if response.status_code in (429, 503):
                        raise ProviderError("rate_limited", 60)
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
                body = _object(
                    json.loads(payload, parse_float=Decimal, parse_constant=_reject_constant)
                )
                if body.get("status") in (0, "failure"):
                    raise ProviderError("not_found")
                product = _object(body.get("product"))
                if normalize_barcode(_text(product.get("code"), 14)) != code:
                    raise ProviderError("invalid_response")
                record, warnings = _record(product, code)
                return SourceDocument(
                    source_id=code,
                    record=record,
                    data_type="Community-reported product label",
                    adapter_version=ADAPTER_VERSION,
                    warnings=warnings,
                    provider="openfoodfacts",
                )
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError("timeout") from None
        except httpx.HTTPError:
            raise ProviderError("unavailable") from None
        except (ValueError, ValidationError, RecursionError):
            raise ProviderError("invalid_response") from None
