"""Synthetic offline barcode/label interpretation; no product or account data."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from nutrition_bot.adapters.nutrition.openfoodfacts import (
    MAX_RESPONSE_BYTES,
    OpenFoodFactsProvider,
)
from nutrition_bot.domain.barcodes import normalize_barcode
from nutrition_bot.domain.food_source import ProviderError, SourceProvenance

CODE = "12345670"


def product(**changes):
    result = {
        "code": CODE,
        "product_name": "Synthetic label product",
        "brands": "Synthetic test brand",
        "nutrition": {
            "input_sets": [
                {
                    "per": "100g",
                    "preparation": "as_sold",
                    "source": "packaging",
                    "per_quantity": 100,
                    "per_unit": "g",
                    "nutrients": {
                        "energy-kcal": {"value_string": "100", "unit": "kcal"},
                        "proteins": {"value_string": "5.25", "unit": "g"},
                        "fat": {"value_string": "0", "unit": "g"},
                        "carbohydrates": {"value_string": "20", "unit": "g"},
                        "vitamin-c": {"value_string": "12", "unit": "mg"},
                        "salt": {"value_string": "0.3", "unit": "g"},
                        "sodium": {"value_computed": "0.12", "unit": "g"},
                    },
                }
            ],
        },
    }
    result.update(changes)
    return result


async def fetch(value=None, *, status=200, headers=None, content=None):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers=headers,
            content=content
            if content is not None
            else json.dumps(
                {"status": "success", "product": product() if value is None else value}
            ).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        document = await OpenFoodFactsProvider(client).fetch(CODE)
    return document, requests


def amounts(document):
    return {item.code: item.amount for item in document.record.nutrients}


async def test_lookup_retains_original_values_units_unknowns_and_explicit_zero():
    document, requests = await fetch()
    assert document.provider == "openfoodfacts" and document.source_id == CODE
    assert document.record.preparation == "as_sold"
    assert document.record.basis_grams == Decimal(100)
    assert amounts(document)["energy"] == Decimal(100)
    assert amounts(document)["protein"] == Decimal("5.25")
    assert amounts(document)["fat"] == 0
    assert amounts(document)["carbohydrate"] is None
    assert amounts(document)["sodium"] is None
    assert amounts(document)["vitamin_c"] == 12
    assert next(n for n in document.record.nutrients if n.code == "vitamin_c").unit == "mg"
    assert "ODbL" in document.record.source_license
    assert any("including fiber" in warning for warning in document.warnings)
    assert len(requests) == 1
    assert requests[0].url.host == "world.openfoodfacts.org"
    assert requests[0].headers["User-Agent"].startswith("BiteClub/")
    assert requests[0].method == "GET"


async def test_explicit_total_carbohydrate_label_establishes_definition():
    value = product()
    value["nutrition"]["input_sets"][0]["nutrients"]["carbohydrates"]["label"] = (
        "Total Carbohydrate"
    )
    document, _ = await fetch(value)
    assert amounts(document)["carbohydrate"] == 20


async def test_source_qualifiers_ius_and_computed_energy_never_become_known_values():
    value = product()
    nutrients = value["nutrition"]["input_sets"][0]["nutrients"]
    nutrients["proteins"]["modifier"] = "<"
    nutrients["vitamin-d"] = {"value_string": "100", "unit": "IU"}
    nutrients["energy-kcal"] = {"value_computed": "100", "unit": "kcal"}
    document, _ = await fetch(value)
    assert amounts(document)["energy"] is None
    assert amounts(document)["protein"] is None
    assert amounts(document)["vitamin_d"] is None


@pytest.mark.parametrize("basis", [None, "serving", "100ml", "unknown"])
async def test_unresolved_mass_basis_requires_manual_label_review(basis):
    with pytest.raises(ProviderError) as error:
        value = product()
        value["nutrition"]["input_sets"][0]["per"] = basis
        await fetch(value)
    assert error.value.code == "unsupported_food"


@pytest.mark.parametrize(
    "status,code",
    [
        (404, "not_found"),
        (429, "rate_limited"),
        (503, "rate_limited"),
        (302, "unavailable"),
        (500, "unavailable"),
    ],
)
async def test_http_failure_is_bounded_without_response_body_leakage(status, code):
    with pytest.raises(ProviderError) as error:
        await fetch(status=status, content=b"Synthetic private response must not leak")
    assert error.value.code == code
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b"invalid json",
        b'{"status": "success", "product": {}}',
        b'{"status": "success", "product": {"code": "12345671"}}',
        b"x" * (MAX_RESPONSE_BYTES + 1),
    ],
)
async def test_invalid_oversized_or_mismatching_source_cannot_be_selected(body):
    with pytest.raises(ProviderError) as error:
        await fetch(content=body)
    assert error.value.code == "invalid_response"


def test_barcode_check_digits_and_normalization_are_deterministic():
    assert normalize_barcode(CODE) == CODE
    assert normalize_barcode("0000012345670") == CODE
    assert normalize_barcode("0000000000017") == "00000017"
    for invalid in ("12345671", "0" * 13, "123", "1234567x", "12345670?", "１２３４５６７０"):
        with pytest.raises(ValueError):
            normalize_barcode(invalid)


def test_provider_specific_source_identity_validation_preserves_usda_contract():
    common = dict(fetched_at=100, adapter_version="synthetic", data_type="synthetic")
    assert SourceProvenance(external_id="123", **common).source_kind == "usda"
    for invalid in ("0", "000123", "1234567890123"):
        with pytest.raises(ValidationError):
            SourceProvenance(external_id=invalid, **common)
    assert (
        SourceProvenance(source_kind="openfoodfacts", external_id=CODE, **common).external_id
        == CODE
    )
    with pytest.raises(ValidationError):
        SourceProvenance(source_kind="openfoodfacts", external_id="12345671", **common)


async def test_selects_one_original_packaging_set_without_combining_source_values():
    value = product()
    sets = value["nutrition"]["input_sets"]
    sets.append(
        {
            "source": "manufacturer",
            "preparation": "as_sold",
            "per": "100g",
            "nutrients": {"energy-kcal": {"value_string": "250", "unit": "kcal"}},
        }
    )
    value["nutrition"]["aggregated_set"] = {"nutrients": {"energy-kcal": {"value": 999}}}
    document, _ = await fetch(value)
    assert amounts(document)["energy"] == 100
    sets.append(sets[0].copy())
    with pytest.raises(ProviderError) as error:
        await fetch(value)
    assert error.value.code == "unsupported_food"


@pytest.mark.parametrize("source", ["estimate", "usda"])
async def test_computed_or_non_label_input_set_requires_manual_review(source):
    value = product()
    value["nutrition"]["input_sets"][0]["source"] = source
    with pytest.raises(ProviderError) as error:
        await fetch(value)
    assert error.value.code == "unsupported_food"


async def test_original_milligram_amount_is_not_replaced_with_normalized_float():
    value = product()
    value["nutrition"]["input_sets"][0]["nutrients"]["sodium"] = {
        "value_string": "123",
        "unit": "mg",
        "value": 0.123,
    }
    document, _ = await fetch(value)
    assert amounts(document)["sodium"] == 123
    assert next(n for n in document.record.nutrients if n.code == "sodium").unit == "mg"
