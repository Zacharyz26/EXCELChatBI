"""模型网关测试：fallback 参数能力过滤（V6）、异常收窄（V7）、adapter 复用（A7/V5）。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import httpx
import pytest
from openai import APIConnectionError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.models.adapters.base import ModelAdapter  # noqa: E402
from packages.models.gateway import ModelGateway  # noqa: E402
from packages.models.registry import (  # noqa: E402
    Defaults,
    ModelRegistry,
    ModelSpec,
    RouteSpec,
)
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402


def _registry() -> ModelRegistry:
    """手工装配的 registry：主选 primary，备选 backup（声明不支持 response_format）。"""
    reg = ModelRegistry("unused.yaml")
    reg._models = {
        "primary": ModelSpec("primary", "p", "m-primary", "", "key"),
        "backup": ModelSpec(
            "backup", "p", "m-backup", "", "key", drop_params=["response_format"]
        ),
    }
    reg._routes = {
        Scenario.CORE_REASONING.value: RouteSpec(
            primary="primary", fallback=["backup"], temperature=0.3
        )
    }
    reg._defaults = Defaults()
    reg._loaded = True
    return reg


class _FailingAdapter:
    """按指定异常失败的假 adapter（缺省为 API 连接错误）。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or APIConnectionError(request=httpx.Request("POST", "http://t"))

    async def complete(self, messages: list[Message], **params: object) -> ModelResponse:
        raise self._exc


class _RecordingAdapter:
    """记录收到的调用参数并返回定值。"""

    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    async def complete(self, messages: list[Message], **params: object) -> ModelResponse:
        self.params = params
        return ModelResponse(content="ok", model="m-backup")


def _seed(gw: ModelGateway, adapters: dict[str, object]) -> None:
    """把假 adapter 塞进网关缓存（绕过真实构造）。"""
    gw._adapters = {k: cast(ModelAdapter, v) for k, v in adapters.items()}


_MSGS = [Message(role="user", content="你好")]
_JSON_MODE: dict[str, object] = {"response_format": {"type": "json_object"}}


# ── V6：按模型能力过滤参数 ──

@pytest.mark.asyncio
async def test_fallback_drops_unsupported_params() -> None:
    """主选挂掉降级到备选时，备选不支持的参数被剥掉，降级真正可用。"""
    gw = ModelGateway(_registry())
    rec = _RecordingAdapter()
    _seed(gw, {"primary": _FailingAdapter(), "backup": rec})

    resp = await gw.complete(Scenario.CORE_REASONING, _MSGS, params=_JSON_MODE)

    assert resp.content == "ok"
    assert rec.params is not None
    assert "response_format" not in rec.params          # 备选声明不支持 → 剥掉
    assert rec.params["temperature"] == 0.3             # 常规参数照传


@pytest.mark.asyncio
async def test_primary_keeps_supported_params() -> None:
    """主选未声明 drop_params，response_format 原样透传。"""
    gw = ModelGateway(_registry())
    rec = _RecordingAdapter()
    _seed(gw, {"primary": rec})

    await gw.complete(Scenario.CORE_REASONING, _MSGS, params=_JSON_MODE)

    assert rec.params is not None
    assert rec.params["response_format"] == {"type": "json_object"}


# ── V7：异常收窄 ──

@pytest.mark.asyncio
async def test_programming_error_is_not_swallowed() -> None:
    """编程错误（KeyError）不得被当作模型不可用而降级掩盖，应正常抛出。"""
    gw = ModelGateway(_registry())
    _seed(gw, {"primary": _FailingAdapter(KeyError("bug")), "backup": _RecordingAdapter()})

    with pytest.raises(KeyError):
        await gw.complete(Scenario.CORE_REASONING, _MSGS)


@pytest.mark.asyncio
async def test_all_candidates_down_raises_runtime_error() -> None:
    """API/网络错误逐一降级，全部失败后统一抛 RuntimeError（供路由映射 502）。"""
    gw = ModelGateway(_registry())
    _seed(gw, {"primary": _FailingAdapter(), "backup": _FailingAdapter()})

    with pytest.raises(RuntimeError, match="所有候选模型均失败"):
        await gw.complete(Scenario.CORE_REASONING, _MSGS)


@pytest.mark.asyncio
async def test_missing_api_key_degrades_to_fallback() -> None:
    """主选 key 缺失（adapter 构造抛 ValueError）→ 降级备选，保持友好降级链。"""
    reg = _registry()
    reg._models["primary"] = ModelSpec("primary", "p", "m-primary", "", "")  # 无 key
    gw = ModelGateway(reg)
    rec = _RecordingAdapter()
    _seed(gw, {"backup": rec})  # primary 未缓存 → 走真实构造 → ValueError → 降级

    resp = await gw.complete(Scenario.CORE_REASONING, _MSGS)

    assert resp.content == "ok"


# ── A7/V5：adapter 缓存复用 ──

@pytest.mark.asyncio
async def test_adapter_constructed_once_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一模型跨请求复用 adapter（连接池），不再每次调用新建。"""
    counter = {"n": 0}

    class _CountingAdapter:
        def __init__(
            self, spec: ModelSpec, timeout_seconds: int = 60, max_retries: int = 2
        ) -> None:
            counter["n"] += 1

        async def complete(self, messages: list[Message], **params: object) -> ModelResponse:
            return ModelResponse(content="ok", model="m")

    monkeypatch.setattr(
        "packages.models.gateway.OpenAICompatibleAdapter", _CountingAdapter
    )
    gw = ModelGateway(_registry())

    await gw.complete(Scenario.CORE_REASONING, _MSGS)
    await gw.complete(Scenario.CORE_REASONING, _MSGS)

    assert counter["n"] == 1
