"""Synthetic USDA payloads; no credentials or external requests are used."""

import asyncio
import gzip
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from nutrition_bot.adapters.nutrition.usda import MAX_RESPONSE_BYTES, USDAProvider
from nutrition_bot.domain.food_source import ProviderError


def nutrient(nutrient_id, amount, unit, **extra):
    return {"nutrient": {"id": nutrient_id, "unitName": unit}, "amount": amount, **extra}


def food(**overrides):
    return {
        "fdcId": 123,
        "description": "Synthetic test food, raw",
        "dataType": "Foundation",
        "publicationDate": "2026-01-01",
        "foodNutrients": [nutrient(1003, 2, "g"), nutrient(1008, 45, "kcal")],
        "foodPortions": [],
        **overrides,
    }


async def fetch(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        return await USDAProvider(client, SecretStr("synthetic-key")).fetch("123")


async def test_search_sends_bounded_generic_query_and_returns_identities_only():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "foods": [
                    food(),
                    food(),
                    food(fdcId=456, description="Synthetic cooked food"),
                    food(fdcId=789, dataType="Branded"),
                    {"fdcId": True, "description": "Invalid", "dataType": "Foundation"},
                    "malformed candidate",
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = USDAProvider(client, SecretStr("synthetic-key"))
        results = await provider.search(" synthetic food ")
        assert len(results) == 2
        assert [item.source_id for item in results] == ["123", "456"]
        assert [item.preparation_hint for item in results] == ["raw", "cooked"]
        assert "nutrients" not in results[0].model_dump()
        assert not client.is_closed
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.nal.usda.gov/fdc/v1/foods/search"
    assert request.headers["X-Api-Key"] == "synthetic-key"
    assert request.headers["Accept-Encoding"] == "identity"
    body = json.loads(request.content)
    assert body == {
        "query": "synthetic food",
        "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
        "pageSize": 10,
        "pageNumber": 1,
    }


async def test_search_bounds_results_and_accepts_empty_result():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"foods": [food(fdcId=i) for i in range(1, 30)]}
            )
        )
    ) as client:
        assert len(await USDAProvider(client, SecretStr("synthetic")).search("food")) == 10
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"foods": []}))
    ) as client:
        assert await USDAProvider(client, SecretStr("synthetic")).search("food") == ()


async def test_full_details_keep_decimal_values_and_never_scale_to_serving():
    payload = json.dumps(food()).replace('"amount": 2', '"amount": 2.123456789')
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        document = await USDAProvider(client, SecretStr("synthetic")).fetch("123")
    assert document.record.basis_grams == 100
    assert {item.code: item.amount for item in document.record.nutrients} == {
        "energy": Decimal(45),
        "protein": Decimal("2.123456789"),
    }
    assert document.source_published_date == "2026-01-01"
    assert document.record.source_license == "CC0-1.0"
    assert "123" in document.record.source_reference
    assert requests[0].url.params == httpx.QueryParams("format=full")


@pytest.mark.parametrize(
    ("energies", "expected", "source_id"),
    [
        ([(1008, 100), (2047, 110), (2048, 105)], 105, 2048),
        ([(1008, 100), (2047, 110)], 110, 2047),
        ([(1008, 100)], 100, 1008),
    ],
)
async def test_energy_source_priority_without_summing_or_macro_recalculation(
    energies, expected, source_id
):
    document = await fetch(
        food(foodNutrients=[nutrient(i, amount, "kcal") for i, amount in energies])
    )
    assert len(document.record.nutrients) == 1
    energy = document.record.nutrients[0]
    assert energy.amount == expected
    assert str(source_id) in energy.note


async def test_missing_energy_never_invented_from_macros():
    document = await fetch(food(foodNutrients=[nutrient(1003, 20, "G")]))
    assert [item.code for item in document.record.nutrients] == ["protein"]


async def test_zero_missing_and_below_quantification_remain_distinct():
    document = await fetch(
        food(
            foodNutrients=[
                nutrient(1003, 2, "g"),
                nutrient(1093, 0, "mg"),
                nutrient(1089, None, "mg"),
                nutrient(1095, 0, "mg", loq="0.03"),
                nutrient(1114, "0.01", "µg", foodNutrientDerivation={"code": "A"}),
            ]
        )
    )
    values = {item.code: item for item in document.record.nutrients}
    assert values["sodium"].amount == 0
    assert values["iron"].amount is None
    assert values["zinc"].amount is None
    assert "0.03 mg" in values["zinc"].note
    assert values["vitamin_d"].amount == Decimal("0.01")
    assert "derivation A" in values["vitamin_d"].note
    assert "calcium" not in values
    assert "quantification_limit_unknown" in document.warnings


@pytest.mark.parametrize("invalid", [True, "NaN", "-1", "1e1000000", "many"])
async def test_invalid_source_amount_is_unknown_not_zero(invalid):
    document = await fetch(
        food(foodNutrients=[nutrient(1003, 2, "g"), nutrient(1093, invalid, "mg")])
    )
    assert next(item for item in document.record.nutrients if item.code == "sodium").amount is None


async def test_unit_mismatch_duplicate_nutrient_and_unsupported_definition_are_not_guessed():
    document = await fetch(
        food(
            foodNutrients=[
                nutrient(1003, 2, "g"),
                nutrient(1114, 40, "IU"),
                nutrient(1093, 10, "mg"),
                nutrient(1093, 15, "mg"),
                nutrient(2033, 5, "g"),
            ]
        )
    )
    values = {item.code: item.amount for item in document.record.nutrients}
    assert values == {"protein": Decimal(2), "sodium": None, "vitamin_d": None}
    assert set(document.warnings) == {
        "nutrient_unit_unknown",
        "duplicate_nutrient_unknown",
        "unsupported_nutrients_omitted",
    }


async def test_preferred_energy_unknown_does_not_fall_back_to_a_different_value():
    document = await fetch(
        food(
            foodNutrients=[
                nutrient(1003, 2, "g"),
                nutrient(2048, 0, "kcal", loq="1"),
                nutrient(1008, 45, "kcal"),
            ]
        )
    )
    assert next(item for item in document.record.nutrients if item.code == "energy").amount is None


@pytest.mark.parametrize(
    ("data_type", "portion", "label"),
    [
        (
            "Foundation",
            {"amount": 2, "measureUnit": {"name": "tablespoon"}, "modifier": "chopped"},
            "2 tablespoon, chopped",
        ),
        (
            "SR Legacy",
            {"amount": 2, "measureUnit": {"name": "undetermined"}, "modifier": "tbsp"},
            "2 tbsp",
        ),
        (
            "Survey (FNDDS)",
            {"modifier": "10205", "portionDescription": "1/2 cup", "amount": None},
            "1/2 cup",
        ),
    ],
)
async def test_portion_uses_whole_measure_grams_and_always_requires_estimate_approval(
    data_type, portion, label
):
    document = await fetch(
        food(dataType=data_type, foodPortions=[{"id": 7, "gramWeight": "32.5", **portion}])
    )
    assert len(document.record.portions) == 1
    result = document.record.portions[0]
    assert result.label == result.original_measure == label
    assert result.grams == Decimal("32.5")
    assert result.is_estimate is True
    assert "portion 7" in result.source


async def test_invalid_or_unlabelled_portions_are_omitted_without_guessing():
    document = await fetch(
        food(
            foodPortions=[
                {"gramWeight": 30},
                {"amount": 1, "measureUnit": {"name": "undetermined"}, "gramWeight": 30},
                {"amount": 1, "measureUnit": {"name": "cup"}, "gramWeight": "-5"},
                {"amount": 1, "measureUnit": {"name": "cup"}, "gramWeight": "1.0001"},
            ]
        )
    )
    assert document.record.portions == ()
    assert document.warnings == ("invalid_portion_omitted",)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Raw or cooked food", "unspecified"),
        ("Food, not cooked", "unspecified"),
        ("Food, uncooked", "raw"),
        ("Food", "unspecified"),
    ],
)
async def test_preparation_hints_are_conservative(description, expected):
    document = await fetch(food(description=description))
    assert document.record.preparation == expected


@pytest.mark.parametrize(
    "source_id", ["0", "-1", "123/../../other", "123?api_key=x", "01", "1" * 100]
)
async def test_invalid_id_cannot_alter_the_endpoint(source_id):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("must not request"))
    ) as client:
        with pytest.raises(ValueError):
            await USDAProvider(client, SecretStr("synthetic")).fetch(source_id)


async def test_not_configured_does_not_make_request():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("must not request"))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, None).fetch("123")
    assert error.value.code == "not_configured"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (403, "unavailable"),
        (503, "unavailable"),
        (429, "rate_limited"),
        (302, "unavailable"),
    ],
)
async def test_http_errors_are_fixed_and_redirects_are_never_followed(status, code):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://untrusted.invalid", "Retry-After": "60"},
            content="secret response detail",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), follow_redirects=True
    ) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, SecretStr("synthetic-key")).fetch("123")
    assert len(requests) == 1
    assert str(error.value) == code
    assert "secret" not in repr(error.value)
    if status == 429:
        assert error.value.retry_after_seconds == 60


@pytest.mark.parametrize(
    ("exception", "code"),
    [(httpx.ReadTimeout("secret"), "timeout"), (httpx.ConnectError("secret"), "unavailable")],
)
async def test_transport_exception_details_do_not_escape(exception, code):
    def transport(request):
        raise exception

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, SecretStr("synthetic")).fetch("123")
    assert str(error.value) == code
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "payload", [b"not json", b"[]", b'{"fdcId": NaN}', b'{"fdcId": Infinity}', b'{"foods": null}']
)
async def test_invalid_search_json_rejected(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, SecretStr("synthetic")).search("food")
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"fdcId": 456}, "invalid_response"),
        ({"dataType": "Branded"}, "unsupported_food"),
        ({"foodNutrients": []}, "unsupported_food"),
        ({"foodNutrients": [nutrient(1003, None, "g")]}, "invalid_response"),
    ],
)
async def test_wrong_identity_or_unusable_record_is_never_importable(changes, code):
    with pytest.raises(ProviderError) as error:
        await fetch(food(**changes))
    assert error.value.code == code


async def test_decoded_response_size_is_bounded_even_when_compressed():
    compressed = gzip.compress(b" " * (MAX_RESPONSE_BYTES + 1))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=compressed
            )
        )
    ) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, SecretStr("synthetic")).fetch("123")
    assert error.value.code == "invalid_response"


async def test_uncompressed_response_size_is_bounded():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))
        )
    ) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, SecretStr("synthetic")).fetch("123")
    assert error.value.code == "invalid_response"


class SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(100):
            await asyncio.sleep(0.01)
            yield b" "


async def test_whole_request_deadline_bounds_a_slow_stream():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await USDAProvider(client, SecretStr("synthetic"), timeout_seconds=0.03).fetch("123")
    assert error.value.code == "timeout"


async def test_caller_cancellation_propagates():
    async def transport(request):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(asyncio.CancelledError):
            await USDAProvider(client, SecretStr("synthetic")).fetch("123")
