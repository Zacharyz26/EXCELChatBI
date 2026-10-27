"""真实适配器和网关的离线用量完整性契约；期望不来自解析实现。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai._models import construct_type
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from packages.models.adapters.openai_compatible import OpenAICompatibleAdapter
from packages.models.gateway import ModelGateway
from packages.models.registry import ModelPricing, ModelRegistry, ModelSpec, RouteSpec
from packages.models.types import Message, ModelResponse, Scenario

CASES = [
    ("complete", {"prompt_tokens": 200, "completion_tokens": 40}, True, (200, 40)),
    ("zero", {"prompt_tokens": 0, "completion_tokens": 0}, True, (0, 0)),
    ("zero_input", {"prompt_tokens": 0, "completion_tokens": 40}, True, (0, 40)),
    ("zero_output", {"prompt_tokens": 200, "completion_tokens": 0}, True, (200, 0)),
    ("missing_completion", {"prompt_tokens": 200}, False, (200, None)),
    ("missing_prompt", {"completion_tokens": 40}, False, (None, 40)),
    ("null_completion", {"prompt_tokens": 200, "completion_tokens": None}, False, (200, None)),
    ("null_prompt", {"prompt_tokens": None, "completion_tokens": 40}, False, (None, 40)),
    ("empty", {}, False, (None, None)),
    ("absent", None, False, (None, None)),
    ("total_only", {"total_tokens": 240}, False, (None, None)),
    ("no_derive", {"prompt_tokens": 200, "total_tokens": 240}, False, (200, None)),
    (
        "contradictory_total",
        {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 239},
        False,
        (200, 40),
    ),
    ("invalid_bool", {"prompt_tokens": True, "completion_tokens": 40}, False, (None, 40)),
    ("invalid_negative", {"prompt_tokens": -1, "completion_tokens": 40}, False, (None, 40)),
    ("invalid_float", {"prompt_tokens": 1.5, "completion_tokens": 40}, False, (None, 40)),
    ("invalid_string", {"prompt_tokens": "200", "completion_tokens": 40}, False, (None, 40)),
    (
        "valid_details",
        {
            "prompt_tokens": 200,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
        True,
        (200, 40),
    ),
    (
        "cache_exceeds_input",
        {
            "prompt_tokens": 200,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 201},
        },
        False,
        (200, 40),
    ),
    (
        "reasoning_exceeds_output",
        {
            "prompt_tokens": 200,
            "completion_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 41},
        },
        False,
        (200, 40),
    ),
    (
        "no_main_from_cache",
        {"completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 200}},
        False,
        (None, 40),
    ),
]


def _gateway(usages: list[Any]) -> tuple[OpenAICompatibleAdapter, ModelGateway, list[bool]]:
    calls: list[bool] = []
    spec = ModelSpec("primary", "synthetic", "offline", "", "")
    spec.pricing = ModelPricing(0.00132, 0.00396, "USD", "2026-07-01")

    async def chunks() -> AsyncIterator[Any]:
        for usage in usages:
            yield construct_type(
                type_=ChatCompletionChunk,
                value={
                    "id": "offline",
                    "created": 0,
                    "model": "offline",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": usage,
                },
            )

    async def create(**kwargs: Any) -> Any:
        calls.append(bool(kwargs.get("stream")))
        if kwargs.get("stream"):
            return chunks()
        return construct_type(
            type_=ChatCompletion,
            value={
                "id": "offline",
                "created": 0,
                "model": "offline",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": usages[-1],
            },
        )

    adapter = OpenAICompatibleAdapter.__new__(OpenAICompatibleAdapter)
    adapter._spec = spec
    adapter._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )  # type: ignore[assignment]
    registry = ModelRegistry("never-read.yaml")
    registry._loaded = True
    registry._models = {"primary": spec}
    registry._routes = {Scenario.AGENT.value: RouteSpec("primary")}
    gateway = ModelGateway(registry)
    gateway._adapters = {"primary": adapter}
    return adapter, gateway, calls


@pytest.mark.parametrize(
    "mode", ["adapter_complete", "adapter_stream", "gateway_complete", "gateway_stream"]
)
@pytest.mark.parametrize("name,usage,available,counts", CASES, ids=[c[0] for c in CASES])
async def test_usage_contract(
    mode: str, name: str, usage: Any, available: bool, counts: tuple[int | None, int | None]
) -> None:
    adapter, gateway, calls = _gateway([usage])
    messages = [Message(role="user", content="synthetic")]
    if mode == "adapter_complete":
        response = await adapter.complete(messages)
    elif mode == "gateway_complete":
        response = await gateway.complete(Scenario.AGENT, messages)
    else:
        stream = (
            adapter.stream_turn(messages)
            if mode == "adapter_stream"
            else gateway.stream_turn(Scenario.AGENT, messages)
        )
        outputs = [v async for v in stream]
        last = outputs[-1]
        assert isinstance(last, ModelResponse)
        response = last
    assert response.usage_available is available
    assert (response.prompt_tokens, response.completion_tokens) == counts
    assert len(calls) == 1
    if mode.startswith("gateway"):
        if available:
            assert counts[0] is not None and counts[1] is not None
            expected = counts[0] * 0.00000132 + counts[1] * 0.00000396
            assert response.cost == pytest.approx(expected)
        else:
            assert response.cost is None


@pytest.mark.parametrize(
    "usages,available",
    [
        ([None, {"prompt_tokens": 200, "completion_tokens": 40}, None], True),
        ([{"prompt_tokens": 200, "completion_tokens": 40}] * 2, True),
        ([{"prompt_tokens": 200}, {"completion_tokens": 40}], False),
        ([{"prompt_tokens": 200}, {"prompt_tokens": 200, "completion_tokens": 40}], False),
        (
            [
                {"prompt_tokens": 200, "completion_tokens": 40},
                {"prompt_tokens": 201, "completion_tokens": 40},
            ],
            False,
        ),
    ],
)
async def test_stream_usage_does_not_merge_or_overwrite_unknown(
    usages: list[Any], available: bool
) -> None:
    _, gateway, calls = _gateway(usages)
    outputs = [v async for v in gateway.stream_turn(Scenario.AGENT, [])]
    response = outputs[-1]
    assert isinstance(response, ModelResponse)
    assert response.usage_available is available
    assert (response.cost is not None) is available
    assert len(calls) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "prompt,known", [(200, True), (0, True), (True, False), ("200", False), (200.0, False)]
)
async def test_actual_sdk_preserves_raw_token_types(prompt: Any, known: bool, stream: bool) -> None:
    """SDK 的宽松 union 转换不可把非法 wire 类型变成可信整数。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raw = {
            "id": "offline",
            "created": 0,
            "model": "offline",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": 40,
                "total_tokens": int(prompt) + 40,
            },
        }
        if stream:
            raw.update(object="chat.completion.chunk", choices=[])
            return httpx.Response(
                200,
                text="data: " + json.dumps(raw) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=raw)

    spec = ModelSpec(
        "offline", "synthetic", "offline", "https://offline.invalid/v1", "synthetic-test-key"
    )
    adapter = OpenAICompatibleAdapter(spec, max_retries=0)
    await adapter._client._client.aclose()
    adapter._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        if stream:
            outputs = [p async for p in adapter.stream_turn([])]
            result = outputs[-1]
            assert isinstance(result, ModelResponse)
        else:
            result = await adapter.complete([])
        assert result.usage_available is known
        assert result.prompt_tokens == (prompt if known else None)
        assert len(requests) == 1
    finally:
        await adapter._client.close()


async def test_missing_and_null_are_distinguishable_from_explicit_zero() -> None:
    results = []
    for usage in (
        {"prompt_tokens": 200},
        {"prompt_tokens": 200, "completion_tokens": None},
        {"prompt_tokens": 200, "completion_tokens": 0},
    ):
        adapter, _, _ = _gateway([usage])
        results.append(await adapter.complete([]))
    assert "missing_completion_tokens" in results[0].usage_issues
    assert "null_completion_tokens" in results[1].usage_issues
    assert results[0].completion_tokens is results[1].completion_tokens is None
    assert results[2].completion_tokens == 0 and results[2].usage_available
