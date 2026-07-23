"""模型网关测试：fallback 参数能力过滤（V6）、异常收窄（V7）、adapter 复用（A7/V5）、
tools 候选跳过与 stream 降级语义（阶段0，设计文档 14.8 / 决策10）。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

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
from packages.models.types import Message, ModelResponse, Scenario, ToolCall  # noqa: E402


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


# ── 阶段0：tools 候选跳过（决策10）──

_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "gen_chart", "parameters": {}}}
]


def _tools_registry() -> ModelRegistry:
    """主选不支持 function calling（如 r1），备选支持。"""
    reg = _registry()
    reg._models["primary"] = ModelSpec(
        "primary", "p", "m-primary", "", "key", supports_tools=False
    )
    return reg


class _ToolRecordingAdapter:
    """记录 tools 与 params 并返回带 tool_calls 的响应。"""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] | None = None
        self.params: dict[str, object] | None = None

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        **params: object,
    ) -> ModelResponse:
        self.tools = tools
        self.params = params
        return ModelResponse(
            content="",
            model="m-backup",
            tool_calls=[ToolCall(id="c1", name="gen_chart", arguments="{}")],
        )


@pytest.mark.asyncio
async def test_tools_skip_unsupported_candidate() -> None:
    """带 tools 的请求跳过 supports_tools=False 的主选，落到支持的备选。

    这是决策10 的核心行为：绝不把 tools 剥掉后静默降级成"不会用工具的聊天"。
    """
    gw = ModelGateway(_tools_registry())
    rec = _ToolRecordingAdapter()
    _seed(gw, {"backup": rec})  # primary 若被调用会走真实构造并失败 → 用例即穿帮

    resp = await gw.complete(Scenario.CORE_REASONING, _MSGS, tools=_TOOLS)

    assert rec.tools == _TOOLS                      # 工具定义原样送达
    assert resp.tool_calls[0].name == "gen_chart"   # tool_calls 透传回调用方
    assert resp.tool_calls[0].arguments == "{}"


@pytest.mark.asyncio
async def test_tools_all_unsupported_raises() -> None:
    """全部候选都不支持工具 → 明确报错，而非退化为普通聊天。"""
    reg = _tools_registry()
    reg._models["backup"] = ModelSpec(
        "backup", "p", "m-backup", "", "key", supports_tools=False
    )
    gw = ModelGateway(reg)

    with pytest.raises(RuntimeError, match="均不支持工具调用"):
        await gw.complete(Scenario.CORE_REASONING, _MSGS, tools=_TOOLS)


@pytest.mark.asyncio
async def test_no_tools_request_still_uses_primary() -> None:
    """不带 tools 的请求不受 supports_tools 影响，主选照常可用。"""
    gw = ModelGateway(_tools_registry())
    rec = _RecordingAdapter()
    _seed(gw, {"primary": rec})

    resp = await gw.complete(Scenario.CORE_REASONING, _MSGS)

    assert resp.content == "ok"


def test_agent_scenario_routes_from_yaml(tmp_path: Path) -> None:
    """Scenario.AGENT 可从 yaml 路由解析，supports_tools 正确加载。"""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        """
providers:
  p: {api_base: "", api_key: "k"}
models:
  chat: {provider: p, model: m-chat}
  reasoner: {provider: p, model: m-r, supports_tools: false}
routes:
  agent: {primary: chat, fallback: []}
""",
        encoding="utf-8",
    )
    reg = ModelRegistry(str(cfg))
    reg.load()

    assert reg.resolve(Scenario.AGENT).primary == "chat"
    assert reg.get_model("chat").supports_tools is True
    assert reg.get_model("reasoner").supports_tools is False


def test_evaluation_route_is_isolated_from_fallback() -> None:
    """独立评测只保留指定模型，不能把 fallback 成功记到候选模型头上。"""
    reg = _registry()

    assert reg.route_candidates(Scenario.CORE_REASONING) == ("primary", "backup")

    isolated = reg.isolated_route(
        Scenario.CORE_REASONING,
        "backup",
        temperature=0.0,
    )
    route = isolated.resolve(Scenario.CORE_REASONING)
    assert route.primary == "backup"
    assert route.fallback == []
    assert route.temperature == 0.0
    assert isolated.route_candidates(Scenario.CORE_REASONING) == ("backup",)
    with pytest.raises(KeyError, match="registry 未配置模型"):
        isolated.get_model("primary")


# ── 阶段0：stream 降级语义 ──


class _StreamAdapter:
    """按脚本产出/失败的流式假 adapter。"""

    def __init__(
        self,
        pieces: list[str] | None = None,
        fail_before_first: bool = False,
        fail_after: int | None = None,
    ) -> None:
        self._pieces = pieces or []
        self._fail_before_first = fail_before_first
        self._fail_after = fail_after

    async def stream(
        self, messages: list[Message], **params: object
    ) -> AsyncIterator[str]:
        if self._fail_before_first:
            raise APIConnectionError(request=httpx.Request("POST", "http://t"))
        for i, p in enumerate(self._pieces):
            if self._fail_after is not None and i >= self._fail_after:
                raise APIConnectionError(request=httpx.Request("POST", "http://t"))
            yield p


async def _collect(gen: AsyncIterator[str]) -> list[str]:
    return [p async for p in gen]


@pytest.mark.asyncio
async def test_stream_yields_pieces() -> None:
    """正常流式：逐段产出主选内容。"""
    gw = ModelGateway(_registry())
    _seed(gw, {"primary": _StreamAdapter(["你", "好"])})

    assert await _collect(gw.stream(Scenario.CORE_REASONING, _MSGS)) == ["你", "好"]


@pytest.mark.asyncio
async def test_stream_falls_back_before_first_chunk() -> None:
    """开流前失败（连接/鉴权）→ 降级到备选，用户无感。"""
    gw = ModelGateway(_registry())
    _seed(
        gw,
        {
            "primary": _StreamAdapter(fail_before_first=True),
            "backup": _StreamAdapter(["降", "级"]),
        },
    )

    assert await _collect(gw.stream(Scenario.CORE_REASONING, _MSGS)) == ["降", "级"]


@pytest.mark.asyncio
async def test_stream_midway_failure_propagates() -> None:
    """已开始产出后中途失败 → 如实上抛，不换模型拼接答案。"""
    gw = ModelGateway(_registry())
    _seed(
        gw,
        {
            "primary": _StreamAdapter(["一半", "后半"], fail_after=1),
            "backup": _StreamAdapter(["不应到这"]),
        },
    )
    out: list[str] = []
    with pytest.raises(APIConnectionError):
        async for p in gw.stream(Scenario.CORE_REASONING, _MSGS):
            out.append(p)
    assert out == ["一半"]  # 已产出的增量不回吞


@pytest.mark.asyncio
async def test_stream_all_down_raises_runtime_error() -> None:
    """所有候选开流前均失败 → RuntimeError（供路由映射 502）。"""
    gw = ModelGateway(_registry())
    _seed(
        gw,
        {
            "primary": _StreamAdapter(fail_before_first=True),
            "backup": _StreamAdapter(fail_before_first=True),
        },
    )
    with pytest.raises(RuntimeError, match="所有候选模型均失败"):
        await _collect(gw.stream(Scenario.CORE_REASONING, _MSGS))


# ── 阶段0：适配器线格式（function calling 消息往返）──

from packages.models.adapters.openai_compatible import (  # noqa: E402
    _parse_tool_calls,
    _to_wire,
)


def test_to_wire_assistant_tool_calls() -> None:
    """assistant 消息的 tool_calls 展开为 OpenAI function calling 线格式。"""
    msgs = [
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="trend", arguments='{"value_col":"销量"}')],
        ),
        Message(role="tool", content='{"ok":true}', tool_call_id="c1"),
    ]
    wire = _to_wire(msgs)

    assert wire[0]["content"] is None  # 空内容 + tool_calls → content 为 null
    assert wire[0]["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "trend", "arguments": '{"value_col":"销量"}'},
        }
    ]
    assert wire[1] == {"role": "tool", "content": '{"ok":true}', "tool_call_id": "c1"}


def test_to_wire_plain_messages_unchanged() -> None:
    """普通消息保持原线格式，不带多余字段（兼容旧调用方）。"""
    wire = _to_wire([Message(role="user", content="你好")])
    assert wire == [{"role": "user", "content": "你好"}]


def test_parse_tool_calls_keeps_raw_arguments() -> None:
    """arguments 保持原样 JSON 字符串（含非法 JSON 也原样保留，交编排层校验）。"""

    class _Fn:
        name = "gen_chart"
        arguments = '{"chart_type": "bar"'  # 故意非法：不在模型层解析

    class _Tc:
        id = "c9"
        function = _Fn()

    class _Msg:
        tool_calls = [_Tc()]

    calls = _parse_tool_calls(_Msg())
    assert calls == [ToolCall(id="c9", name="gen_chart", arguments='{"chart_type": "bar"')]


def test_parse_tool_calls_none_is_empty() -> None:
    """无 tool_calls（纯文本答复）→ 空列表，不报错。"""

    class _Msg:
        tool_calls = None

    assert _parse_tool_calls(_Msg()) == []


# ── 阶段3：stream_turn（带 tools 的流式轮次，Agent 循环用）──


class _StreamTurnAdapter:
    """按脚本产出文本增量 + 末尾 ModelResponse 的假 adapter。"""

    def __init__(
        self,
        pieces: list[str] | None = None,
        tool_calls: list[ToolCall] | None = None,
        fail_before_first: bool = False,
    ) -> None:
        self._pieces = pieces or []
        self._tool_calls = tool_calls or []
        self._fail_before_first = fail_before_first
        self.tools: list[dict[str, Any]] | None = None

    async def stream_turn(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        **params: object,
    ) -> AsyncIterator[str | ModelResponse]:
        self.tools = tools
        if self._fail_before_first:
            raise APIConnectionError(request=httpx.Request("POST", "http://t"))
        for p in self._pieces:
            yield p
        yield ModelResponse(
            content="".join(self._pieces), model="m", tool_calls=self._tool_calls
        )


async def _collect_turn(
    gen: AsyncIterator[str | ModelResponse],
) -> tuple[list[str], ModelResponse | None]:
    pieces: list[str] = []
    final: ModelResponse | None = None
    async for item in gen:
        if isinstance(item, ModelResponse):
            final = item
        else:
            pieces.append(item)
    return pieces, final


@pytest.mark.asyncio
async def test_stream_turn_yields_text_then_response() -> None:
    """正常轮次：文本增量逐段产出，最后一项是聚合 ModelResponse。"""
    gw = ModelGateway(_registry())
    calls = [ToolCall(id="c1", name="gen_chart", arguments="{}")]
    _seed(gw, {"primary": _StreamTurnAdapter(["你", "好"], tool_calls=calls)})

    pieces, final = await _collect_turn(
        gw.stream_turn(Scenario.CORE_REASONING, _MSGS, tools=_TOOLS)
    )

    assert pieces == ["你", "好"]
    assert final is not None
    assert final.content == "你好"
    assert final.tool_calls == calls


@pytest.mark.asyncio
async def test_stream_turn_skips_unsupported_candidate_with_tools() -> None:
    """带 tools 时跳过 supports_tools=False 的主选（决策10），tools 原样送达备选。"""
    gw = ModelGateway(_tools_registry())
    rec = _StreamTurnAdapter(["答"])
    _seed(gw, {"backup": rec})  # primary 若被调用会走真实构造并失败 → 用例即穿帮

    pieces, final = await _collect_turn(
        gw.stream_turn(Scenario.CORE_REASONING, _MSGS, tools=_TOOLS)
    )

    assert pieces == ["答"]
    assert final is not None and final.tool_calls == []
    assert rec.tools == _TOOLS


@pytest.mark.asyncio
async def test_stream_turn_all_unsupported_raises() -> None:
    """全部候选都不支持工具 → 明确报错，不退化成普通聊天。"""
    reg = _tools_registry()
    reg._models["backup"] = ModelSpec(
        "backup", "p", "m-backup", "", "key", supports_tools=False
    )
    gw = ModelGateway(reg)

    with pytest.raises(RuntimeError, match="均不支持工具调用"):
        await _collect_turn(gw.stream_turn(Scenario.CORE_REASONING, _MSGS, tools=_TOOLS))


@pytest.mark.asyncio
async def test_stream_turn_falls_back_before_first_chunk() -> None:
    """开流前失败 → 降级到备选，语义与 stream 一致。"""
    gw = ModelGateway(_registry())
    _seed(
        gw,
        {
            "primary": _StreamTurnAdapter(fail_before_first=True),
            "backup": _StreamTurnAdapter(["降级"]),
        },
    )

    pieces, final = await _collect_turn(gw.stream_turn(Scenario.CORE_REASONING, _MSGS))

    assert pieces == ["降级"]
    assert final is not None and final.model == "m"


# ── 阶段3：适配器 stream_turn 的 tool_calls 增量聚合 ──


def _chunk(content: str | None = None, tool_deltas: list[Any] | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_deltas))]
    )


def _tool_delta(
    index: int, id: str | None = None, name: str | None = None, arguments: str | None = None
) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


@pytest.mark.asyncio
async def test_adapter_stream_turn_accumulates_tool_call_fragments() -> None:
    """OpenAI 流式 tool_calls 分片按 index 聚合，arguments 原样拼接（红线3 归编排层）。"""
    from types import SimpleNamespace

    from packages.models.adapters.openai_compatible import OpenAICompatibleAdapter

    chunks = [
        _chunk(content="我先"),
        _chunk(content="看看"),
        _chunk(tool_deltas=[_tool_delta(0, id="c1", name="gen_chart", arguments='{"x"')]),
        _chunk(tool_deltas=[_tool_delta(0, arguments=':"月份"}')]),
        _chunk(tool_deltas=[_tool_delta(1, id="c2", name="kb_search", arguments="{}")]),
    ]

    class _FakeStream:
        def __aiter__(self) -> Any:
            self._it = iter(chunks)
            return self

        async def __anext__(self) -> Any:
            try:
                return next(self._it)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    created: dict[str, Any] = {}

    class _FakeCompletions:
        async def create(self, **kwargs: Any) -> Any:
            created.update(kwargs)
            return _FakeStream()

    adapter = OpenAICompatibleAdapter(ModelSpec("n", "p", "m-x", "", "key"))
    adapter._client = cast(
        Any, SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    )

    pieces, final = await _collect_turn(adapter.stream_turn(_MSGS, tools=_TOOLS))

    assert created["stream"] is True
    assert created["tools"] == _TOOLS
    assert pieces == ["我先", "看看"]
    assert final is not None
    assert final.content == "我先看看"
    assert [(c.id, c.name, c.arguments) for c in final.tool_calls] == [
        ("c1", "gen_chart", '{"x":"月份"}'),
        ("c2", "kb_search", "{}"),
    ]
