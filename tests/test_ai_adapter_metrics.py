"""Content-free provider measurements from synthetic offline responses only."""

import asyncio
import json
from dataclasses import asdict

import httpx
import pytest

from nutrition_bot.adapters.ai.chatgpt_auth import save_credentials
from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter
from nutrition_bot.adapters.ai.openrouter import AiTransportError
from nutrition_bot.domain.ai import AiCallMetrics, AiUnavailable, known_token_count
from tests.test_ai_drafts import adapter_with, completion, proposed, reviewed_route
from tests.test_chatgpt_plan_ai import credentials, policy, private_path, stream


def usage(**changes):
    value = {
        "input_tokens": 120,
        "output_tokens": 85,
        "input_tokens_details": {"cached_tokens": 40},
        "output_tokens_details": {"reasoning_tokens": 20},
    }
    value.update(changes)
    return value


async def plan_result(tmp_path, data, metrics, *, status=200):
    path = private_path(tmp_path)
    save_credentials(path, credentials())

    def transport(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
            )
        return httpx.Response(status, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        return await adapter.complete(
            role="meal_text", body={"model": "synthetic-model-v1"}, metrics=metrics
        )


async def test_plan_completed_usage_and_stages_are_content_free(tmp_path):
    metrics = AiCallMetrics()
    result = await plan_result(tmp_path, stream(usage=usage()), metrics)
    assert result.intent == "meal"
    assert metrics.model == "synthetic-model-v1"
    assert metrics.prompt_version == "meal-extraction-v2"
    assert metrics.schema_version == "meal-proposal-v1"
    assert metrics.reasoning_effort == "default"
    assert metrics.inference_sent and metrics.failure_category is None
    assert (
        metrics.input_tokens,
        metrics.output_tokens,
        metrics.cached_tokens,
        metrics.reasoning_tokens,
    ) == (120, 85, 40, 20)
    assert all(
        type(value) is int and value >= 0
        for value in (metrics.auth_ms, metrics.model_catalog_ms, metrics.inference_ms)
    )
    serialized = json.dumps(asdict(metrics))
    assert "synthetic-access-token" not in serialized
    assert "Interpreted from input" not in serialized
    assert "synthetic-item" not in serialized


@pytest.mark.parametrize("value", [None, True, -1, 2.0, "12", {}, [], 2**63])
async def test_plan_invalid_usage_stays_unknown_without_changing_validity(tmp_path, value):
    metrics = AiCallMetrics()
    result = await plan_result(
        tmp_path,
        stream(
            usage=usage(
                input_tokens=value,
                output_tokens=value,
                input_tokens_details={"cached_tokens": value},
                output_tokens_details={"reasoning_tokens": value},
            )
        ),
        metrics,
    )
    assert result.intent == "meal"
    assert (
        metrics.input_tokens
        is metrics.output_tokens
        is metrics.cached_tokens
        is metrics.reasoning_tokens
        is None
    )
    assert metrics.failure_category is None


async def test_plan_missing_usage_stays_unknown(tmp_path):
    metrics = AiCallMetrics()
    await plan_result(tmp_path, stream(), metrics)
    assert metrics.input_tokens is metrics.output_tokens is None
    assert metrics.reasoning_tokens is metrics.cached_tokens is None


async def test_plan_valid_usage_retained_when_completed_output_fails_schema(tmp_path):
    metrics = AiCallMetrics()
    with pytest.raises(AiUnavailable):
        await plan_result(
            tmp_path, stream(proposed(nutrients={"energy": 123}), usage=usage()), metrics
        )
    assert metrics.input_tokens == 120 and metrics.output_tokens == 85
    assert metrics.failure_category == "schema" and metrics.inference_sent
    assert metrics.inference_ms is not None


@pytest.mark.parametrize(
    ("input_total", "output_total", "cached", "reasoning"),
    [(10, 5, 11, 6), (None, None, 1, 1), (10, 5, True, -1)],
)
async def test_plan_inconsistent_usage_details_stay_unknown(
    tmp_path, input_total, output_total, cached, reasoning
):
    metrics = AiCallMetrics()
    await plan_result(
        tmp_path,
        stream(
            usage=usage(
                input_tokens=input_total,
                output_tokens=output_total,
                input_tokens_details={"cached_tokens": cached},
                output_tokens_details={"reasoning_tokens": reasoning},
            )
        ),
        metrics,
    )
    assert metrics.cached_tokens is metrics.reasoning_tokens is None


async def test_plan_completed_refusal_keeps_trustworthy_usage(tmp_path):
    metrics = AiCallMetrics()
    refusal = b'data: {"type":"response.refusal.done","refusal":"synthetic-private-text"}\n\n'
    with pytest.raises(AiUnavailable):
        await plan_result(tmp_path, refusal + stream(usage=usage()), metrics)
    assert metrics.failure_category == "refusal"
    assert metrics.input_tokens == 120 and metrics.output_tokens == 85
    assert "synthetic-private-text" not in json.dumps(asdict(metrics))


@pytest.mark.parametrize(
    "code",
    ["subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"],
)
async def test_plan_subscription_sse_usage_limits_have_bounded_category(tmp_path, code):
    metrics = AiCallMetrics()
    data = ("data: " + json.dumps({"type": "error", "error": {"code": code}}) + "\n\n").encode()
    with pytest.raises(AiUnavailable):
        await plan_result(tmp_path, data, metrics)
    assert metrics.failure_category == "usage_limit" and metrics.input_tokens is None


@pytest.mark.parametrize(
    ("data", "category"),
    [
        (stream(status="incomplete", usage=usage()), "incomplete"),
        (stream(model="synthetic-other", usage=usage()), "policy"),
        (stream(usage=usage()) + stream(usage=usage()), "schema"),
        (
            b'data: {"type":"response.incomplete","response":{"usage":{"input_tokens":120}}}\n\n',
            "incomplete",
        ),
        (b'data: {"type":"response.refusal.done"}\n\n', "refusal"),
        (b'data: {"type":"error","error":{"code":"rate_limit_exceeded"}}\n\n', "usage_limit"),
        (
            b'data: {"type":"response.failed","response":{"error":{"code":"quota_exceeded"}}}\n\n',
            "usage_limit",
        ),
        (
            b'data: {"type":"response.failed","response":{"error":'
            b'{"code":"synthetic-private-error"}}}\n\n',
            "incomplete",
        ),
        (b'data: {"type":"response.completed","response":null}\n\n', "schema"),
        (b"data: [DONE]\n\n", "incomplete"),
    ],
)
async def test_plan_untrusted_or_partial_usage_is_not_counted(tmp_path, data, category):
    metrics = AiCallMetrics()
    with pytest.raises(AiUnavailable):
        await plan_result(tmp_path, data, metrics)
    assert metrics.failure_category == category
    assert metrics.input_tokens is metrics.output_tokens is None
    assert metrics.cached_tokens is metrics.reasoning_tokens is None


@pytest.mark.parametrize(
    ("status", "category"), [(401, "http"), (429, "usage_limit"), (503, "http")]
)
async def test_plan_http_failure_does_not_count_response_body_usage(tmp_path, status, category):
    metrics = AiCallMetrics()
    with pytest.raises(AiUnavailable):
        await plan_result(tmp_path, stream(usage=usage()), metrics, status=status)
    assert metrics.failure_category == category and metrics.inference_sent
    assert metrics.input_tokens is None and metrics.inference_ms is not None


@pytest.mark.parametrize(("stage", "category"), [("auth", "auth"), ("catalog", "catalog")])
async def test_plan_pre_inference_failures_measure_stage_without_sent_call(
    tmp_path, stage, category
):
    path = private_path(tmp_path)
    if stage == "catalog":
        save_credentials(path, credentials())
    metrics = AiCallMetrics()
    calls = []

    def transport(request):
        calls.append(request.method)
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        with pytest.raises(AiUnavailable):
            await adapter.complete(role="meal_text", body={}, metrics=metrics)
    assert metrics.failure_category == category
    assert not metrics.inference_sent and metrics.inference_ms is None
    assert metrics.auth_ms is not None
    assert (metrics.model_catalog_ms is not None) == (stage == "catalog")
    assert calls == (["GET"] if stage == "catalog" else [])


@pytest.mark.parametrize(
    ("error", "category"),
    [(httpx.ConnectError("synthetic"), "network"), (httpx.ReadTimeout("synthetic"), "timeout")],
)
async def test_plan_transport_failures_keep_unknown_usage(tmp_path, error, category):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    metrics = AiCallMetrics()

    def transport(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
            )
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        with pytest.raises(AiUnavailable):
            await adapter.complete(role="meal_text", body={}, metrics=metrics)
    assert metrics.failure_category == category and metrics.inference_sent
    assert metrics.input_tokens is None and metrics.inference_ms is not None


async def test_openrouter_complete_usage_includes_known_details_and_charge():
    adapter, client = adapter_with(
        lambda request: httpx.Response(
            200,
            json=completion(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "cost": 0.001,
                    "prompt_tokens_details": {"cached_tokens": 30},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                }
            ),
        )
    )
    metrics = AiCallMetrics()
    try:
        result = await adapter.complete(reviewed_route(), {}, metrics=metrics)
    finally:
        await client.aclose()
    assert result.proposal is not None
    assert (
        metrics.input_tokens,
        metrics.output_tokens,
        metrics.cached_tokens,
        metrics.reasoning_tokens,
    ) == (100, 50, 30, 5)
    assert metrics.charged_micro_usd == result.charged_micro_usd == 1000
    assert metrics.model == "synthetic/model-v1" and metrics.failure_category is None
    assert metrics.inference_sent and metrics.inference_ms is not None
    assert metrics.auth_ms is metrics.model_catalog_ms is None


@pytest.mark.parametrize(
    ("error", "category"),
    [(httpx.ConnectError("synthetic"), "network"), (httpx.ReadTimeout("synthetic"), "timeout")],
)
async def test_openrouter_transport_failure_measurement_keeps_existing_exception(error, category):
    def transport(request):
        raise error

    adapter, client = adapter_with(transport)
    metrics = AiCallMetrics()
    try:
        with pytest.raises(AiTransportError):
            await adapter.complete(reviewed_route(), {}, metrics=metrics)
    finally:
        await client.aclose()
    assert metrics.failure_category == category and metrics.inference_sent
    assert metrics.input_tokens is metrics.charged_micro_usd is None
    assert metrics.inference_ms is not None


async def test_openrouter_invalid_counts_stay_unknown_and_preserve_usage_fault():
    adapter, client = adapter_with(
        lambda request: httpx.Response(
            200,
            json=completion(
                usage={
                    "prompt_tokens": True,
                    "completion_tokens": -1,
                    "cost": 0.001,
                    "prompt_tokens_details": {"cached_tokens": "10"},
                    "completion_tokens_details": {"reasoning_tokens": 0.5},
                }
            ),
        )
    )
    metrics = AiCallMetrics()
    try:
        result = await adapter.complete(reviewed_route(), {}, metrics=metrics)
    finally:
        await client.aclose()
    assert result.fault == metrics.failure_category == "usage"
    assert (
        metrics.input_tokens
        is metrics.output_tokens
        is metrics.cached_tokens
        is metrics.reasoning_tokens
        is None
    )


async def test_cancellation_records_inference_time_without_swallowing_cancellation(tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    metrics = AiCallMetrics()
    entered = asyncio.Event()
    waiting = asyncio.Event()

    async def transport(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
            )
        entered.set()
        await waiting.wait()
        return httpx.Response(200, content=stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        task = asyncio.create_task(adapter.complete(role="meal_text", body={}, metrics=metrics))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert metrics.inference_sent and metrics.inference_ms is not None
    assert metrics.failure_category == "timeout" and metrics.input_tokens is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1, 1),
        (2**63 - 1, 2**63 - 1),
        (None, None),
        (True, None),
        (-1, None),
        (1.0, None),
        (2**63, None),
    ],
)
def test_known_token_counts_preserve_zero_and_reject_coercions(value, expected):
    assert known_token_count(value) == expected
