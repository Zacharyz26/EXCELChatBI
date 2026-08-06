"""阶段3：Agent 循环测试。

覆盖：/chat/stream SSE 协议与持久化（吸收原阶段1 用例）、上下文装配（数据集
清单 + 分析登记表）、工具轮事件序列与工件落库、带错重试、同参熔断、调用数
上限、对话锁。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import model_gateway_dep, session_store_dep, settings_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from apps.orchestrator import agent_loop as agent_loop_module  # noqa: E402
from apps.orchestrator.agent_loop import (  # noqa: E402
    AgentLoopConfig,
    ConversationLockPool,
    _enrich_tool_arguments,
    _preserve_host_reference_assumptions,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import AgentToolRegistry  # noqa: E402
from apps.orchestrator.control.contracts import build_minimal_contract  # noqa: E402
from apps.orchestrator.control.planner_contract import validate_task_plan  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.common.client_gateway import (  # noqa: E402
    GatewayHealth,
    MCPExecutionResult,
    MCPTransportName,
)
from mcp_servers.common.contracts import (  # noqa: E402
    MCPRequestContext,
    MCPToolDescriptor,
    ToolCapabilityMetadata,
)
from packages.common.config import Settings  # noqa: E402
from packages.governance.permissions import Principal  # noqa: E402
from packages.governance.policy import ToolPolicyGateway  # noqa: E402
from packages.governance.schema_validator import SchemaValidationError  # noqa: E402
from packages.models.types import Message as ModelMessage  # noqa: E402
from packages.models.types import ModelResponse, Scenario, ToolCall  # noqa: E402
from packages.session.compaction import CompactionStore  # noqa: E402
from packages.session.coref import (  # noqa: E402
    ReferenceResolver,
    find_reference_assumption,
)
from packages.session.memory_models import MemoryDraft  # noqa: E402
from packages.session.memory_refs import (  # noqa: E402
    MemoryReferenceResolver,
    find_memory_reference_assumptions,
    memory_reference_semantic_key,
    memory_reference_summary,
)
from packages.session.memory_store import MemoryStore  # noqa: E402
from packages.session.models import Conversation  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402
from packages.session.task_store import TaskStore  # noqa: E402
from sse_starlette.sse import AppStatus  # noqa: E402

_DATASET_REF = "d" * 32
_REPORT_ID = "e" * 32
_TIMEOUT_TEST_RUN_BUDGET_SECONDS = 30


class ScriptedGateway:
    """按脚本逐轮返回的假网关：记录每轮的消息与 tools。

    turns 每项：{deltas: [str], tool_calls: [ToolCall], error: Exception|None,
    fail_after_deltas: bool}；content 为 deltas 拼接。
    """

    def __init__(self, turns: list[dict[str, Any]] | None = None) -> None:
        self.turns = list(turns or [{"deltas": ["你好", "，有什么可以帮你？"]}])
        self.calls: list[dict[str, Any]] = []

    async def stream_turn(
        self,
        scenario: Scenario,
        messages: list[ModelMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str | ModelResponse]:
        del params
        self.calls.append({"scenario": scenario, "messages": list(messages), "tools": tools})
        turn = self.turns.pop(0)
        error = turn.get("error")
        if error is not None and not turn.get("fail_after_deltas"):
            raise error
        delay = turn.get("delay")
        if isinstance(delay, int | float) and delay > 0:
            await asyncio.sleep(delay)
        deltas: list[str] = turn.get("deltas", [])
        for piece in deltas:
            yield piece
        if error is not None:
            raise error
        yield ModelResponse(
            content="".join(deltas),
            model="scripted",
            tool_calls=list(turn.get("tool_calls", [])),
        )


class PlannerAwareGateway(ScriptedGateway):
    """同时提供结构化 Planner 与流式 Executor 的测试网关。"""

    def __init__(self, turns: list[dict[str, Any]], planner_plan: dict[str, Any]) -> None:
        super().__init__(turns)
        self.planner_plan = planner_plan
        self.planner_calls = 0
        self.planner_messages: list[list[ModelMessage]] = []

    async def complete(
        self,
        scenario: Scenario,
        messages: list[ModelMessage],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        assert scenario == Scenario.COMPLEX_REASONING
        assert params is not None
        assert messages
        self.planner_calls += 1
        self.planner_messages.append(list(messages))
        return ModelResponse(
            content=json.dumps(self.planner_plan, ensure_ascii=False),
            model="eligible-planner",
        )


class FakeRegistry:
    """确定性工具注册表替身：按工具名执行 handler。"""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self._handlers = handlers
        self.executed: list[tuple[str, str]] = []
        self.remote_catalog_validations = 0
        self.catalog_watch_started = False

    async def validate_remote_catalog(self) -> dict[str, str]:
        self.remote_catalog_validations += 1
        return {"fake": "a" * 64}

    def start_catalog_watch(self) -> None:
        self.catalog_watch_started = True

    def openai_tools(
        self,
        *,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in self._handlers
            if allowed_tool_names is None or name in allowed_tool_names
        ]

    def capability_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": capability,
                "description": name,
                "allowed": True,
                "risk": "low",
                "read_only": True,
                "artifact_types": [],
            }
            for name in self._handlers
            for capability in self.capabilities_for_tool(name)
        ]

    def openai_tools_for_capabilities(
        self,
        capabilities: set[str],
        *,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in self.openai_tools(allowed_tool_names=allowed_tool_names)
            if capabilities.intersection(self.capabilities_for_tool(str(item["function"]["name"])))
        ]

    def capability_catalog_snapshot(self) -> dict[str, Any]:
        capabilities = self.capability_catalog()
        for item in capabilities:
            item["tool_names"] = [
                name
                for name in self._handlers
                if item["name"] in self.capabilities_for_tool(name)
            ]
        return {
            "schema": "chatbi-capability-catalog-v1",
            "capabilities": capabilities,
            "tools": [
                {
                    "tool_name": name,
                    "service_name": "fake",
                    "tool_version": "1.0.0",
                    "contract_hash": hashlib.sha256(name.encode()).hexdigest(),
                    "capabilities": list(self.capabilities_for_tool(name)),
                }
                for name in self._handlers
            ],
        }

    def validate_capability_catalog_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> tuple[str, ...]:
        current = {
            item["tool_name"]: item
            for item in self.capability_catalog_snapshot()["tools"]
        }
        return tuple(
            f"tool_unavailable:{item['tool_name']}"
            for item in snapshot["tools"]
            if item["tool_name"] not in current
        )

    @staticmethod
    def capability_catalog_from_snapshot(
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return list(snapshot["capabilities"])

    @staticmethod
    def tool_names_from_snapshot(snapshot: dict[str, Any]) -> frozenset[str]:
        return frozenset(item["tool_name"] for item in snapshot["tools"])

    def capabilities_for_tool(self, tool_name: str) -> tuple[str, ...]:
        mapping = {
            "get_data_profile": "data.profile",
            "trend_analysis": "stats.trend",
            "anomaly_detect": "stats.anomaly",
            "regression": "stats.regression",
            "correlation": "stats.correlation",
            "gen_chart": "visualization.chart",
            "chart_screenshot": "visualization.screenshot",
            "transform_dataset": "dataset.transform",
            "aggregate_preview": "data.aggregate",
            "kb_search": "knowledge.search",
            "generate_report": "report.generate",
        }
        capability = mapping.get(tool_name)
        return (capability,) if capability is not None else ()

    def execute(self, name: str, arguments_json: str) -> Any:
        self.executed.append((name, arguments_json))
        return self._handlers[name](json.loads(arguments_json or "{}"))


class MCPGatewayRegistry(FakeRegistry):
    """Prove the production loop uses execute_mcp and Host-owned context."""

    def __init__(
        self,
        result: dict[str, Any],
        *,
        transport: MCPTransportName = "stdio",
    ) -> None:
        super().__init__({"get_data_profile": lambda _: result})
        self.result = result
        self.transport = transport
        self.contexts: list[MCPRequestContext] = []

    async def execute_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        context: MCPRequestContext,
        *,
        timeout_seconds: float,
    ) -> MCPExecutionResult:
        assert name == "get_data_profile"
        assert arguments == {"dataset_ref": _DATASET_REF}
        assert timeout_seconds > 0
        self.contexts.append(context)
        return MCPExecutionResult(
            result=self.result,
            transport=self.transport,
            degraded=False,
            health=GatewayHealth("healthy", self.transport, generation=1),
        )

    def execute(self, name: str, arguments_json: str) -> Any:
        raise AssertionError("production loop must not call compatibility execute")

    def audit_metadata_for_tool(self, tool_name: str) -> dict[str, Any]:
        assert tool_name == "get_data_profile"
        return {
            "service_name": "data-tools",
            "tool_name": tool_name,
            "tool_version": "1.0.0",
            "risk_level": "low",
            "required_permissions": ["analysis:execute"],
            "artifact_types": ["profile"],
            "read_only": True,
            "idempotent": True,
            "contract_hash": "a" * 64,
        }


class HighRiskMCPGatewayRegistry(MCPGatewayRegistry):
    """声明 high 风险的画像工具，用于验证审批恢复执行链。"""

    def mcp_descriptor_for_tool(
        self,
        tool_name: str,
    ) -> MCPToolDescriptor | None:
        if tool_name != "get_data_profile":
            return None
        return MCPToolDescriptor(
            name=tool_name,
            description="读取高风险画像",
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_ref": {"type": "string"},
                },
                "required": ["dataset_ref"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            metadata=ToolCapabilityMetadata(
                capabilities=("data.profile",),
                risk_level="high",
            ),
        )


class WriteMCPGatewayRegistry(MCPGatewayRegistry):
    """将画像工具声明为写操作，用于验证标准只读自主等级。"""

    def mcp_descriptor_for_tool(
        self,
        tool_name: str,
    ) -> MCPToolDescriptor | None:
        if tool_name != "get_data_profile":
            return None
        return MCPToolDescriptor(
            name=tool_name,
            description="模拟带副作用的数据画像",
            input_schema={
                "type": "object",
                "properties": {"dataset_ref": {"type": "string"}},
                "required": ["dataset_ref"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            metadata=ToolCapabilityMetadata(
                capabilities=("data.profile",),
                risk_level="medium",
                read_only=False,
            ),
        )


def _events(raw: list[dict[str, str]]) -> list[tuple[str, dict[str, Any]]]:
    return [(item["event"], json.loads(item["data"])) for item in raw]


async def _run_loop(
    store: SessionStore,
    conversation: Conversation,
    gateway: ScriptedGateway,
    registry: Any,
    user_text: str = "分析一下",
    config: AgentLoopConfig | None = None,
    policy: ToolPolicyGateway | None = None,
    planner_gateway: Any | None = None,
    enforce_plan: bool | None = None,
    autonomy_mode: str = "autonomous",
    parent_run_id: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    raw = [
        item
        async for item in stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text=user_text,
            store=store,
            gateway=cast(Any, gateway),
            registry=cast(AgentToolRegistry, registry),
            locks=ConversationLockPool(),
            config=config or AgentLoopConfig(tool_result_max_chars=500),
            policy=policy,
            planner_gateway=planner_gateway,
            enforce_plan=(planner_gateway is not None if enforce_plan is None else enforce_plan),
            autonomy_mode=cast(Any, autonomy_mode),
            parent_run_id=parent_run_id,
        )
    ]
    return _events(raw)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "chatbi.db"))


@pytest.fixture
def conversation(store: SessionStore) -> Conversation:
    project = store.create_project("测试项目")
    return store.create_conversation(project.id)


def _register_dataset(
    store: SessionStore,
    conversation: Conversation,
    ref: str = _DATASET_REF,
) -> None:
    store.register_dataset(
        ref=ref,
        project_id=conversation.project_id,
        filename="销售.xlsx",
        profile={
            "row_count": 3,
            "column_count": 2,
            "columns": [{"name": "月份"}, {"name": "销售额"}],
        },
    )


def _remember_agent_reference(
    store: SessionStore,
    conversation: Conversation,
    *,
    alias: str,
    target_ref: str,
    key: str,
    scope: str = "project",
    kind: str = "entity_mapping",
    canonical_field: str | None = None,
) -> str:
    source = store.append_message(
        conversation_id=conversation.id,
        role="user",
        content=f"确认引用 {alias} {key}",
    )
    memories = MemoryStore(store)
    result = memories.remember(
        project_id=conversation.project_id,
        principal=Principal(user_id="local-user"),
        draft=MemoryDraft(
            scope=scope,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            semantic_key=memory_reference_semantic_key(
                kind=kind,  # type: ignore[arg-type]
                alias=alias,
            ),
            content_summary=memory_reference_summary(
                kind=kind,  # type: ignore[arg-type]
                alias=alias,
                canonical_field=canonical_field,
            ),
            source_type="user_confirmation",
            source_ref=source.id,
            source_hash=hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
            confidence=0.95,
            conversation_id=(conversation.id if scope == "conversation" else None),
        ),
        idempotency_key=f"agent-reference-{key}",
    )
    memories.add_link(
        result.record.memory_id,
        project_id=conversation.project_id,
        principal=Principal(user_id="local-user"),
        target_type="dataset",
        target_ref=target_ref,
    )
    return result.record.memory_id


# ── 工具轮：事件序列、工件落库、结果回填 ──


@pytest.mark.asyncio
async def test_tool_round_emits_transparency_events_and_persists(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_threadpool(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(agent_loop_module, "run_in_threadpool", direct_threadpool)
    _register_dataset(store, conversation)
    profile_result = {
        "profile": {"row_count": 3, "column_count": 2},
        "quality": {"duplicate_rows": 0},
    }
    registry = FakeRegistry({"get_data_profile": lambda args: profile_result})
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["我先获取数据画像"],
                "tool_calls": [
                    ToolCall(
                        id="c1",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            },
            {"deltas": ["结论：", "共 3 行。"]},
        ]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    assert registry.remote_catalog_validations == 1
    assert registry.catalog_watch_started is True
    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "run.started",
        "text.delta",  # 工具轮开场白流式吐出
        "understanding",  # 轮末转为理解卡
        "plan",
        "step.started",
        "tool_start",
        "artifact",
        "step.completed",
        "tool_end",
        "verification.started",
        "verification",
        "text.delta",  # 最终答复流式
        "text.delta",
        "done",
    ], events
    by_name = dict(events)
    run_id = cast(str, by_name["meta"]["run_id"])
    assert by_name["goal"]["run_id"] == run_id
    assert by_name["verification"]["payload"]["verdict"] == "PASS"
    assert by_name["understanding"]["text"] == "我先获取数据画像"
    assert by_name["plan"]["steps"][0]["tool"] == "get_data_profile"
    # 执行卡默认展示人话参数摘要，原始 JSON 仅供调参表单
    assert by_name["tool_start"]["fields"] == f"数据集: {_DATASET_REF[:8]}"
    assert by_name["tool_start"]["args_preview"] == (f'{{"dataset_ref":"{_DATASET_REF}"}}')
    assert by_name["tool_end"]["status"] == "ok"
    assert "3 行" in by_name["tool_end"]["summary"]
    assert by_name["done"]["tool_calls"] == 1
    task_store = TaskStore(store.db_path)
    run = task_store.get_run(run_id)
    assert run is not None and run.status == "completed"
    assert run.usage == {
        "tool_calls": 1,
        "tool_attempts": 1,
        "invalid_tool_calls": 0,
    }
    assert len(task_store.list_evidence(run_id)) == 1
    claims = task_store.list_claims(run_id)
    assert len(claims) == 1
    assert claims[0].statement == "结论：共 3 行。"
    assert len(claims[0].evidence_ids) == 1
    assert claims[0].value_refs[0]["path"] == "$.profile.row_count"
    assert [event.sequence for event in task_store.list_events(run_id)] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
    ]
    step_started = by_name["step.started"]
    assert step_started["payload"]["policy"]["allowed"] is True
    assert "arguments" not in step_started["payload"]
    step_completed = by_name["step.completed"]
    assert step_completed["payload"]["status"] == "completed"
    assert len(step_completed["payload"]["evidence_ids"]) == 1
    assert step_completed["payload"]["observation"]["status"] == "ok"

    # 工件：类型/来源/analysis_id/数据集归属
    artifacts = store.list_artifacts(conversation.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == "profile"
    assert artifact.source_tool == "get_data_profile"
    assert artifact.dataset_ref == _DATASET_REF
    assert artifact.params is not None and artifact.params["analysis_id"]
    assert by_name["artifact"]["id"] == artifact.id

    # 消息：user + 工具轮 assistant（带 tool_calls）+ 每步结果（role=tool，供历史
    # 执行卡精确回放）+ 最终 assistant；工具结果原文不落消息表
    messages = store.list_messages(conversation.id)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls == [
        {
            "id": "c1",
            "name": "get_data_profile",
            "arguments": f'{{"dataset_ref":"{_DATASET_REF}"}}',
        }
    ]
    outcome = json.loads(messages[2].content)
    assert outcome["tool_call_id"] == "c1"
    assert outcome["status"] == "ok"
    assert "3 行" in outcome["summary"]
    assert outcome["fields"] == f"数据集: {_DATASET_REF[:8]}"  # 历史执行卡直接复用人话摘要
    assert messages[3].content == "结论：共 3 行。"

    # 第二轮模型请求里回填了 tool 结果
    second_call = gateway.calls[1]["messages"]
    tool_messages = [m for m in second_call if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "c1"
    assert '"duplicate_rows":0' in tool_messages[0].content


@pytest.mark.asyncio
async def test_assisted_mode_pauses_after_plan_until_explicit_resume(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_threadpool(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(agent_loop_module, "run_in_threadpool", direct_threadpool)
    _register_dataset(store, conversation)
    events = await _run_loop(
        store,
        conversation,
        ScriptedGateway([]),
        FakeRegistry(
            {"get_data_profile": lambda _: {"profile": {"row_count": 3}}}
        ),
        user_text="查看数据画像",
        enforce_plan=True,
        autonomy_mode="assisted",
    )

    names = [name for name, _ in events]
    assert names[-2:] == ["autonomy.plan_review_requested", "done"]
    assert "run.started" not in names
    meta = dict(events)["meta"]
    assert meta["autonomy_mode"] == "assisted"
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(cast(str, meta["run_id"]))
    assert run is not None
    assert run.status == "paused"
    assert run.autonomy_mode == "assisted"
    assert tasks.list_invocations(run.run_id) == []


@pytest.mark.asyncio
async def test_resume_fails_closed_when_frozen_tool_is_unavailable(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_threadpool(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(agent_loop_module, "run_in_threadpool", direct_threadpool)
    _register_dataset(store, conversation)
    initial_events = await _run_loop(
        store,
        conversation,
        ScriptedGateway([]),
        FakeRegistry({"get_data_profile": lambda _: {}}),
        user_text="查看数据画像",
        enforce_plan=True,
        autonomy_mode="assisted",
    )
    run_id = cast(str, dict(initial_events)["meta"]["run_id"])
    tasks = TaskStore(store.db_path)
    paused = tasks.get_run(run_id)
    assert paused is not None and paused.status == "paused"
    resumed, _, _ = tasks.control_transition(
        run_id,
        expected_version=paused.state_version,
        idempotency_key="resume-with-catalog-drift",
        command="resume",
        allowed_statuses={"paused"},
        status="running",
        event_type="run.resumed",
        payload={"reason": "catalog_drift_test"},
        require_checkpoint=True,
    )
    raw = [
        item
        async for item in stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text="查看数据画像",
            store=store,
            gateway=cast(Any, ScriptedGateway([])),
            registry=cast(AgentToolRegistry, FakeRegistry({})),
            locks=ConversationLockPool(),
            config=AgentLoopConfig(tool_result_max_chars=500),
            run_id=resumed.run_id,
            resume_existing=True,
        )
    ]
    events = _events(raw)

    error = next(payload for name, payload in events if name == "error")
    assert error["code"] == "capability_catalog_drift"
    assert error["issues"] == ["tool_unavailable:get_data_profile"]
    failed = tasks.get_run(run_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.terminal_reason == "capability_catalog_drift"
    assert tasks.list_invocations(run_id) == []


@pytest.mark.asyncio
async def test_analysis_branch_sends_bounded_parent_feedback_to_llm_planner(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_threadpool(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(agent_loop_module, "run_in_threadpool", direct_threadpool)
    _register_dataset(store, conversation)
    parent_events = await _run_loop(
        store,
        conversation,
        ScriptedGateway([{"deltas": ["基线答复已完成。"]}]),
        FakeRegistry({"get_data_profile": lambda _: {}}),
        user_text="完成基线答复",
        enforce_plan=False,
    )
    parent_run_id = cast(str, dict(parent_events)["meta"]["run_id"])
    tasks = TaskStore(store.db_path)
    parent = tasks.get_run(parent_run_id)
    assert parent is not None and parent.status == "completed"
    tasks.record_user_feedback(
        parent_run_id,
        expected_version=parent.state_version,
        idempotency_key="branch-feedback-planner-context",
        subject_user_id="local-user",
        rating="not_helpful",
        comment="COMPOSE_4D_FEEDBACK 请保留原始数据，只重新核对字段",
    )
    planner_plan = {
        "schema_version": 1,
        "summary": "重新核对画像",
        "steps": [
            {
                "step_id": "profile_feedback_branch",
                "purpose": "重新核对字段与规模",
                "capability": "data.profile",
                "dependencies": [],
                "expected_evidence": ["画像 Evidence"],
                "completion_conditions": ["画像工具成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    gateway = PlannerAwareGateway([], planner_plan)

    child_events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({"get_data_profile": lambda _: {}}),
        user_text="COMPOSE_4D_BRANCH 请深入分析当前数据画像",
        planner_gateway=gateway,
        autonomy_mode="assisted",
        parent_run_id=parent_run_id,
    )

    assert gateway.planner_calls == 1
    request = json.loads(gateway.planner_messages[0][-1].content)
    planning_request = request["planning_request"]
    assert "COMPOSE_4D_BRANCH" in planning_request
    assert "COMPOSE_4D_FEEDBACK" in planning_request
    assert "不能扩大数据、工具或权限范围" in planning_request
    assert request["contract"]["goal"] == "COMPOSE_4D_BRANCH 请深入分析当前数据画像"
    assert [name for name, _ in child_events][-2:] == [
        "autonomy.plan_review_requested",
        "done",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("descriptor_mode", ["missing", "write"])
async def test_read_only_mode_persists_policy_denial_without_executing_write_tool(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_mode: str,
) -> None:
    async def direct_threadpool(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(agent_loop_module, "run_in_threadpool", direct_threadpool)
    _register_dataset(store, conversation)
    result = {
        "profile": {"row_count": 3, "column_count": 2},
        "quality": {"duplicate_rows": 0},
    }
    registry = (
        WriteMCPGatewayRegistry(result)
        if descriptor_mode == "write"
        else MCPGatewayRegistry(result)
    )
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["尝试执行写操作"],
                "tool_calls": [
                    ToolCall(
                        id="read-only-write-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            },
            {"deltas": ["标准只读模式已阻止该操作。"], "tool_calls": []},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="验证标准只读边界",
        autonomy_mode="read_only",
    )

    assert registry.contexts == []
    meta = dict(events)["meta"]
    assert meta["autonomy_mode"] == "read_only"
    tasks = TaskStore(store.db_path)
    run_id = cast(str, meta["run_id"])
    run = tasks.get_run(run_id)
    assert run is not None and run.autonomy_mode == "read_only"
    invocations = tasks.list_invocations(run_id)
    assert len(invocations) == 1
    assert invocations[0].status == "failed"
    snapshot = tasks.get_snapshot(run_id)
    assert snapshot is not None
    assert snapshot["last_observation"]["code"] == "autonomy_write_denied"
    assert any(
        name == "tool_end"
        and payload.get("message", "").startswith("未执行：标准只读模式")
        for name, payload in events
    )


@pytest.mark.asyncio
async def test_high_risk_tool_pauses_then_consumes_approval_after_host_recovery(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_threadpool(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(
        agent_loop_module,
        "run_in_threadpool",
        direct_threadpool,
    )
    _register_dataset(store, conversation)
    result = {
        "profile": {"row_count": 3, "column_count": 2},
        "quality": {"duplicate_rows": 0},
    }
    registry = HighRiskMCPGatewayRegistry(result)
    initial_gateway = ScriptedGateway(
        [
            {
                "deltas": ["准备读取高风险画像"],
                "tool_calls": [
                    ToolCall(
                        id="approval-call-before-restart",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            }
        ]
    )

    paused_events = await _run_loop(
        store,
        conversation,
        initial_gateway,
        registry,
        user_text="查看数据画像",
        enforce_plan=True,
    )

    paused_names = [name for name, _ in paused_events]
    assert "approval.requested" in paused_names
    assert "approval_required" in paused_names
    assert paused_names[-1] == "done"
    meta = dict(paused_events)["meta"]
    run_id = cast(str, meta["run_id"])
    tasks = TaskStore(store.db_path)
    paused = tasks.get_run(run_id)
    assert paused is not None and paused.status == "paused"
    assert tasks.list_invocations(run_id) == []
    assert registry.contexts == []
    approvals = tasks.list_approvals(
        run_id,
        tenant_id="local",
        subject_user_id="local-user",
    )
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.status == "pending"

    approved_run, approved, _, _ = tasks.decide_approval(
        approval.approval_id,
        expected_run_version=paused.state_version,
        expected_approval_version=approval.version,
        idempotency_key="approve-after-host-restart",
        tenant_id="local",
        actor_user_id="local-user",
        decision="approved",
        reason="确认读取该数据集画像",
    )
    resumed, _, _ = tasks.control_transition(
        run_id,
        expected_version=approved_run.state_version,
        idempotency_key="resume-approved-after-host-restart",
        command="resume",
        allowed_statuses={"paused"},
        status="running",
        event_type="run.resumed",
        payload={"reason": "approval_granted"},
        require_checkpoint=True,
    )
    resumed_gateway = ScriptedGateway(
        [
            {
                "deltas": ["恢复并读取已批准画像"],
                "tool_calls": [
                    ToolCall(
                        id="approval-call-after-restart",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            },
            {"deltas": ["结论：共 3 行。"]},
        ]
    )
    resumed_raw = [
        item
        async for item in stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text="查看数据画像",
            store=store,
            gateway=cast(Any, resumed_gateway),
            registry=cast(AgentToolRegistry, registry),
            locks=ConversationLockPool(),
            config=AgentLoopConfig(tool_result_max_chars=500),
            principal=Principal(user_id="local-user"),
            run_id=resumed.run_id,
            resume_existing=True,
        )
    ]
    resumed_events = _events(resumed_raw)

    resumed_names = [name for name, _ in resumed_events]
    assert resumed_names.index("approval.consumed") < resumed_names.index(
        "step.started"
    )
    saved = tasks.get_run(run_id)
    assert saved is not None and saved.status == "completed"
    consumed = tasks.get_approval(approval.approval_id)
    assert consumed is not None
    assert consumed.status == "consumed" and consumed.version == approved.version + 1
    assert len(tasks.list_invocations(run_id)) == 1
    assert len(registry.contexts) == 1
    context = registry.contexts[0]
    assert context.approval_id == approval.approval_id
    assert context.approval_version == consumed.version
    assert context.approval_contract_hash == approval.tool_schema_hash
    assert context.approval_parameter_hash == approval.parameter_summary_hash


@pytest.mark.asyncio
async def test_executor_uses_mcp_context_and_transports_have_equivalent_evidence(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    _register_dataset(store, conversation)
    registry = MCPGatewayRegistry(
        {
            "profile": {"row_count": 3, "column_count": 2},
            "quality": {"duplicate_rows": 0},
        }
    )
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["读取画像"],
                "tool_calls": [
                    ToolCall(
                        id="mcp-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            },
            {"deltas": ["共有 3 行。"]},
        ]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    by_name = dict(events)
    run_id = cast(str, by_name["meta"]["run_id"])
    assert by_name["tool_end"]["transport"] == "stdio"
    assert by_name["tool_end"]["degraded"] is False
    assert len(registry.contexts) == 1
    context = registry.contexts[0]
    assert context.project_id == conversation.project_id
    assert context.conversation_id == conversation.id
    assert context.run_id == run_id
    assert context.invocation_id
    assert context.idempotency_key
    assert context.permission_snapshot_id
    assert context.memory_snapshot_id == by_name["meta"]["memory_snapshot_id"]
    assert context.evidence_ledger_version == 0
    evidence = TaskStore(store.db_path).list_evidence(run_id)
    assert len(evidence) == 1
    assert evidence[0].source["transport"] == "stdio"
    assert evidence[0].source["mcp_execution"] == "canonical_gateway"
    assert evidence[0].source["mcp_gateway_health"] == "healthy"
    assert evidence[0].source["mcp_gateway_generation"] == 1
    assert evidence[0].source["tool_contract"] == {
        "service_name": "data-tools",
        "tool_name": "get_data_profile",
        "tool_version": "1.0.0",
        "risk_level": "low",
        "required_permissions": ["analysis:execute"],
        "artifact_types": ["profile"],
        "read_only": True,
        "idempotent": True,
        "contract_hash": "a" * 64,
    }
    started = next(payload for name, payload in events if name == "step.started")
    assert started["payload"]["policy"]["tool_contract"] == evidence[0].source[
        "tool_contract"
    ]

    http_conversation = store.create_conversation(conversation.project_id)
    http_registry = MCPGatewayRegistry(
        registry.result,
        transport="streamable_http",
    )
    http_gateway = ScriptedGateway(
        [
            {
                "deltas": ["读取画像"],
                "tool_calls": [
                    ToolCall(
                        id="mcp-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            },
            {"deltas": ["共有 3 行。"]},
        ]
    )
    http_events = await _run_loop(
        store,
        http_conversation,
        http_gateway,
        http_registry,
    )
    http_run_id = cast(str, dict(http_events)["meta"]["run_id"])
    http_evidence = TaskStore(store.db_path).list_evidence(http_run_id)
    assert http_evidence[0].source["transport"] == "streamable_http"
    assert http_evidence[0].result_hash == evidence[0].result_hash

    stdio_invocation = TaskStore(store.db_path).list_invocations(run_id)[0]
    http_invocation = TaskStore(store.db_path).list_invocations(http_run_id)[0]
    assert (
        stdio_invocation.tool_name,
        stdio_invocation.args,
        stdio_invocation.status,
    ) == (
        http_invocation.tool_name,
        http_invocation.args,
        http_invocation.status,
    )
    stdio_artifact = store.list_artifacts(conversation.id)[0]
    http_artifact = store.list_artifacts(http_conversation.id)[0]
    assert (
        stdio_artifact.type,
        stdio_artifact.source_tool,
        stdio_artifact.dataset_ref,
        stdio_artifact.payload,
    ) == (
        http_artifact.type,
        http_artifact.source_tool,
        http_artifact.dataset_ref,
        http_artifact.payload,
    )


@pytest.mark.asyncio
async def test_production_planner_limits_tools_and_binds_invocation_to_step(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    plan = {
        "schema_version": 1,
        "summary": "检查数据质量",
        "steps": [
            {
                "step_id": "profile",
                "purpose": "取得质量画像",
                "capability": "data.profile",
                "dependencies": [],
                "expected_evidence": ["画像 Evidence"],
                "completion_conditions": ["画像调用成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    gateway = PlannerAwareGateway(
        [
            {
                "deltas": ["我先检查质量"],
                "tool_calls": [
                    ToolCall(
                        id="planned-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ],
            },
            {"deltas": ["共有 3 行。"]},
        ],
        plan,
    )
    registry = FakeRegistry(
        {
            "get_data_profile": lambda _: {"profile": {"row_count": 3}},
            "anomaly_detect": lambda _: {"anomalies": []},
        }
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="先检查数据质量，然后给出结论",
        planner_gateway=gateway,
    )

    assert gateway.planner_calls == 1
    offered_names = {
        str(item["function"]["name"])
        for item in cast(list[dict[str, Any]], gateway.calls[0]["tools"])
    }
    assert offered_names == {"get_data_profile"}
    by_name = dict(events)
    plan_payload = cast(dict[str, Any], by_name["plan.created"]["payload"])
    assert plan_payload["planner"]["route"] == "llm"
    run_id = cast(str, by_name["meta"]["run_id"])
    task_store = TaskStore(store.db_path)
    steps = task_store.list_plan_steps(run_id)
    assert len(steps) == 1
    assert steps[0].logical_id == "profile"
    assert steps[0].status == "completed"
    invocation = task_store.list_invocations(run_id)[0]
    assert invocation.step_id == steps[0].step_id


@pytest.mark.asyncio
async def test_dependency_executor_only_offers_the_current_ready_frontier(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    plan = {
        "schema_version": 1,
        "summary": "先画像再分析趋势",
        "steps": [
            {
                "step_id": "profile",
                "purpose": "取得数据画像",
                "capability": "data.profile",
                "dependencies": [],
                "expected_evidence": ["画像 Evidence"],
                "completion_conditions": ["画像调用成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            },
            {
                "step_id": "trend",
                "purpose": "分析趋势",
                "capability": "stats.trend",
                "dependencies": ["profile"],
                "expected_evidence": ["趋势 Evidence"],
                "completion_conditions": ["趋势调用成功"],
                "fallback": [{"when": "失败", "action": "correct_parameters"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }
    gateway = PlannerAwareGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="profile-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {
                "tool_calls": [
                    ToolCall(
                        id="trend-call",
                        name="trend_analysis",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {"deltas": ["计划中的分析已经完成。"]},
        ],
        plan,
    )
    registry = FakeRegistry(
        {
            "get_data_profile": lambda _: {"profile": {"row_count": 3}},
            "trend_analysis": lambda _: {"series": [{"period": "一月"}]},
        }
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="先检查数据规模，然后分析趋势",
        planner_gateway=gateway,
    )

    offered = [
        {str(item["function"]["name"]) for item in cast(list[dict[str, Any]], call["tools"] or [])}
        for call in gateway.calls
    ]
    assert offered == [
        {"get_data_profile"},
        {"trend_analysis"},
        set(),
    ]
    assert [item[0] for item in registry.executed] == [
        "get_data_profile",
        "trend_analysis",
    ]
    run_id = cast(str, dict(events)["meta"]["run_id"])
    assert [
        (step.logical_id, step.status) for step in TaskStore(store.db_path).list_plan_steps(run_id)
    ] == [("profile", "completed"), ("trend", "completed")]
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_duplicate_guard_does_not_block_distinct_steps_using_same_tool(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    steps = [
        {
            "step_id": "schema_profile",
            "purpose": "确认字段结构",
            "capability": "data.profile",
            "dependencies": [],
            "expected_evidence": ["字段 Evidence"],
            "completion_conditions": ["字段画像成功"],
            "fallback": [{"when": "失败", "action": "retry"}],
        },
        {
            "step_id": "quality_profile",
            "purpose": "确认质量概况",
            "capability": "data.profile",
            "dependencies": ["schema_profile"],
            "expected_evidence": ["质量 Evidence"],
            "completion_conditions": ["质量画像成功"],
            "fallback": [{"when": "失败", "action": "retry"}],
        },
    ]
    gateway = PlannerAwareGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="schema-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {
                "tool_calls": [
                    ToolCall(
                        id="quality-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {"deltas": ["两个画像步骤都已完成。"]},
        ],
        {
            "schema_version": 1,
            "summary": "分步检查结构和质量",
            "steps": steps,
            "assumptions": [],
            "clarifications": [],
        },
    )
    registry = FakeRegistry({"get_data_profile": lambda _: {"profile": {"row_count": 3}}})

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="先确认字段结构，然后再检查质量并给出替代解释",
        planner_gateway=gateway,
    )

    assert [item[0] for item in registry.executed] == [
        "get_data_profile",
        "get_data_profile",
    ]
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_retryable_failure_creates_new_plan_version_and_recovers(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    plan = {
        "schema_version": 1,
        "summary": "分析趋势",
        "steps": [
            {
                "step_id": "trend",
                "purpose": "分析趋势",
                "capability": "stats.trend",
                "dependencies": [],
                "expected_evidence": ["趋势 Evidence"],
                "completion_conditions": ["趋势调用成功"],
                "fallback": [{"when": "参数不适用", "action": "correct_parameters"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    attempts = 0

    def trend_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("时间列不存在")
        return {"series": [{"period": arguments.get("time_col", "月份")}]}

    gateway = PlannerAwareGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="trend-bad",
                        name="trend_analysis",
                        arguments=(f'{{"dataset_ref":"{_DATASET_REF}",' '"time_col":"错误列"}'),
                    )
                ]
            },
            {
                "tool_calls": [
                    ToolCall(
                        id="trend-fixed",
                        name="trend_analysis",
                        arguments=(f'{{"dataset_ref":"{_DATASET_REF}",' '"time_col":"月份"}'),
                    )
                ]
            },
            {"deltas": ["修正参数后已完成趋势分析。"]},
        ],
        plan,
    )
    registry = FakeRegistry({"trend_analysis": trend_handler})

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="先分析趋势，然后给出替代解释",
        planner_gateway=gateway,
    )

    names = [name for name, _ in events]
    assert "replanning.started" in names
    assert "plan.revised" in names
    assert "replanning.completed" in names
    run_id = cast(str, dict(events)["meta"]["run_id"])
    task_store = TaskStore(store.db_path)
    run = task_store.get_run(run_id)
    assert run is not None
    assert run.plan_version == 2
    assert run.status == "completed"
    assert task_store.list_plan_steps(run_id)[0].status == "completed"
    attempts_by_event = [
        payload["payload"]["attempt"] for name, payload in events if name == "step.started"
    ]
    assert attempts_by_event == [1, 2]


@pytest.mark.asyncio
async def test_zero_anomaly_observation_skips_conditional_transform(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    plan = {
        "schema_version": 1,
        "summary": "检测异常并按需清洗",
        "steps": [
            {
                "step_id": "detect",
                "purpose": "检测异常",
                "capability": "stats.anomaly",
                "dependencies": [],
                "expected_evidence": ["异常 Evidence"],
                "completion_conditions": ["异常检测成功"],
                "fallback": [{"when": "失败", "action": "correct_parameters"}],
            },
            {
                "step_id": "clean",
                "purpose": "仅在存在异常时清洗",
                "capability": "dataset.transform",
                "dependencies": ["detect"],
                "expected_evidence": ["衍生数据集"],
                "completion_conditions": ["清洗成功或条件跳过"],
                "fallback": [{"when": "失败", "action": "block"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }
    gateway = PlannerAwareGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="detect-call",
                        name="anomaly_detect",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {"deltas": ["未发现需要清洗的异常，条件流程已经结束。"]},
        ],
        plan,
    )
    registry = FakeRegistry(
        {
            "anomaly_detect": lambda _: {"n_anomalies": 0, "anomalies": []},
            "transform_dataset": lambda _: {"dataset_ref": "derived"},
        }
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="先检测异常，然后在需要时清洗并给出替代解释",
        planner_gateway=gateway,
    )

    assert [item[0] for item in registry.executed] == ["anomaly_detect"]
    run_id = cast(str, dict(events)["meta"]["run_id"])
    task_store = TaskStore(store.db_path)
    run = task_store.get_run(run_id)
    assert run is not None and run.plan_version == 2
    assert [(step.logical_id, step.status) for step in task_store.list_plan_steps(run_id)] == [
        ("detect", "completed"),
        ("clean", "skipped"),
    ]
    assert "plan.revised" in [name for name, _ in events]
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_plan_enforcement_stays_fail_closed_without_llm_planner_gateway(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="outside-plan",
                        name="anomaly_detect",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}","columns":["销售额"]}}',
                    )
                ]
            },
            {"deltas": ["已经完成。"]},
            {"deltas": ["仍然无法执行计划外工具。"]},
        ]
    )
    registry = FakeRegistry(
        {
            "get_data_profile": lambda _: {"profile": {"row_count": 3}},
            "anomaly_detect": lambda _: {"anomalies": []},
        }
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="介绍这份数据的规模和质量",
        enforce_plan=True,
    )

    offered_names = {
        str(item["function"]["name"])
        for item in cast(list[dict[str, Any]], gateway.calls[0]["tools"])
    }
    assert offered_names == {"get_data_profile"}
    assert registry.executed == []
    tool_end = next(payload for name, payload in events if name == "tool_end")
    assert tool_end["status"] == "error"
    assert "不属于当前持久化计划" in tool_end["message"]
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "incomplete_plan"


@pytest.mark.asyncio
async def test_model_timeout_persists_failed_terminal_state(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    gateway = ScriptedGateway([{"delay": 0.2, "deltas": ["太晚了"]}])
    config = AgentLoopConfig(
        model_timeout_seconds=0.01,  # type: ignore[arg-type]
        # 隔离模型超时语义，避免上下文准备耗时与整轮超时竞争。
        run_timeout_seconds=_TIMEOUT_TEST_RUN_BUDGET_SECONDS,
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        config=config,
    )

    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "model_timeout"
    run_id = cast(str, events[-1][1]["run_id"])
    run = TaskStore(store.db_path).get_run(run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.terminal_reason == "model_timeout"


@pytest.mark.asyncio
async def test_tool_timeout_is_unknown_and_blocks_automatic_retry(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    def slow_search(_args: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.2)
        return {"is_empty": True, "hits": []}

    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="slow-search",
                        name="kb_search",
                        arguments='{"query":"测试"}',
                    )
                ]
            }
        ]
    )
    config = AgentLoopConfig(
        tool_timeout_seconds=0.01,  # type: ignore[arg-type]
        # 隔离工具超时语义，给 unknown/blocked 终态留出持久化余量。
        run_timeout_seconds=_TIMEOUT_TEST_RUN_BUDGET_SECONDS,
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({"kb_search": slow_search}),
        config=config,
    )

    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "tool_timeout"
    run_id = cast(str, events[-1][1]["run_id"])
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(run_id)
    assert run is not None and run.status == "blocked"
    assert run.terminal_reason == "tool_timeout"
    assert tasks.list_invocations(run_id)[0].status == "unknown"


@pytest.mark.asyncio
async def test_ambiguous_metric_waits_for_user_without_calling_model(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    store.register_dataset(
        ref=_DATASET_REF,
        project_id=conversation.project_id,
        filename="销售.xlsx",
        profile={
            "row_count": 12,
            "column_count": 4,
            "columns": [
                {"name": "日期"},
                {"name": "地区"},
                {"name": "销售额"},
                {"name": "销量"},
            ],
        },
    )
    gateway = ScriptedGateway([{"deltas": ["不应调用模型"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="比较各地区的销售趋势。",
    )

    assert gateway.calls == []
    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "waiting_user",
        "text.delta",
        "done",
    ], events
    waiting = dict(events)["waiting_user"]
    assert waiting["payload"]["about"] == "metric"
    assert "销售额、销量" in dict(events)["text.delta"]["delta"]
    done = dict(events)["done"]
    assert done["run_status"] == "waiting_user"
    run = TaskStore(store.db_path).get_run(cast(str, done["run_id"]))
    assert run is not None and run.status == "waiting_user"


@pytest.mark.asyncio
async def test_stream_disconnect_cancels_active_run(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    stream = stream_agent_chat(
        conversation_id=conversation.id,
        project_id=conversation.project_id,
        user_text="分析一下",
        store=store,
        gateway=cast(Any, ScriptedGateway([{"delay": 0.2, "deltas": ["晚"]}])),
        registry=cast(AgentToolRegistry, FakeRegistry({})),
        locks=ConversationLockPool(),
        config=AgentLoopConfig(run_timeout_seconds=2),
    )

    meta = await anext(stream)
    run_id = cast(str, json.loads(meta["data"])["run_id"])
    await stream.aclose()

    run = TaskStore(store.db_path).get_run(run_id)
    assert run is not None
    assert run.status == "cancelled"
    assert run.terminal_reason in {"stream_disconnected", "stream_closed"}


@pytest.mark.asyncio
async def test_unsupported_numeric_claim_is_corrected_before_delivery(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    registry = FakeRegistry(
        {"get_data_profile": lambda args: {"profile": {"row_count": 3, "column_count": 2}}}
    )
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="profile-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {"deltas": ["数据共有 4 行。"]},
            {"deltas": ["经工具结果核对，数据共有 3 行。"]},
        ]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    streamed = "".join(payload["delta"] for name, payload in events if name == "text.delta")
    assert "4 行" not in streamed
    assert "3 行" in streamed
    assert len(gateway.calls) == 3
    correction = gateway.calls[2]["messages"][-1]
    assert correction.role == "user"
    assert "数字无法在当前工具 Evidence 中定位" in correction.content
    verification_events = [payload for name, payload in events if name == "verification"]
    assert verification_events[0]["payload"]["checks"][0]["code"] == ("unsupported_numeric_claim")
    assert verification_events[-1]["payload"]["verdict"] == "PASS"
    run_id = cast(str, dict(events)["done"]["run_id"])
    claims = TaskStore(store.db_path).list_claims(run_id)
    assert claims[0].statement == "经工具结果核对，数据共有 3 行。"
    assert claims[0].value_refs[0]["supported"] is True


@pytest.mark.asyncio
async def test_unsupported_numeric_claim_is_removed_after_retry_is_exhausted(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    registry = FakeRegistry({"get_data_profile": lambda args: {"profile": {"row_count": 3}}})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="profile-call",
                        name="get_data_profile",
                        arguments=f'{{"dataset_ref":"{_DATASET_REF}"}}',
                    )
                ]
            },
            {"deltas": ["数据共有 4 行。"]},
            {"deltas": ["数据共有 5 行。"]},
        ]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    assert events[-1][0] == "done"
    assert events[-1][1]["run_status"] == "completed"
    streamed = "".join(payload["delta"] for name, payload in events if name == "text.delta")
    assert "5 行" not in streamed
    assert "3 行" in streamed
    assert "已省略无法由当前工具证据直接支持的派生数字" in streamed
    run_id = cast(str, events[-1][1]["run_id"])
    run = TaskStore(store.db_path).get_run(run_id)
    assert run is not None and run.status == "completed"
    assert [message.role for message in store.list_messages(conversation.id)] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_kb_search_persists_traceable_citation_artifact(
    store: SessionStore, conversation: Conversation
) -> None:
    result = {
        "is_empty": False,
        "hits": [
            {
                "source": "指标口径.md",
                "section": "活跃用户",
                "text": "活跃用户指统计周期内有效登录的去重用户数。",
            }
        ],
    }
    registry = FakeRegistry({"kb_search": lambda args: result})
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["我先查询指标口径。"],
                "tool_calls": [
                    ToolCall(
                        id="kb-call",
                        name="kb_search",
                        arguments='{"query":"活跃用户怎么定义"}',
                    )
                ],
            },
            {"deltas": ["活跃用户是有效登录的去重用户数（来源：指标口径.md）。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="活跃用户怎么定义？",
    )

    artifact_event = next(payload for name, payload in events if name == "artifact")
    assert artifact_event["type"] == "citations"
    assert artifact_event["payload"]["hits"][0]["source"] == "指标口径.md"
    artifact = store.list_artifacts(conversation.id)[0]
    assert artifact.type == "citations"
    assert artifact.source_tool == "kb_search"


@pytest.mark.asyncio
async def test_domain_definition_lookup_joins_the_evidence_ledger(
    store: SessionStore, conversation: Conversation
) -> None:
    result = {
        "status": "resolved",
        "is_empty": False,
        "requires_clarification": False,
        "semantic_key": "metric.grouped_measure",
        "as_of": "2026-06-01T00:00:00Z",
        "definition": {
            "version": 1,
            "title": "匿名分组度量",
            "description": "按匿名分组汇总匿名度量。",
            "source": "urn:domain-definition:grouped-measure:v1",
        },
        "candidates": [],
        "compilation_status": "not_requested",
        "compiled_invocation": None,
    }
    registry = FakeRegistry({"domain_definition_lookup": lambda args: result})
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["我先解析项目中的版本化指标定义。"],
                "tool_calls": [
                    ToolCall(
                        id="definition-call",
                        name="domain_definition_lookup",
                        arguments='{"semantic_key":"metric.grouped_measure"}',
                    )
                ],
            },
            {
                "deltas": [
                    "已解析版本 1（来源：urn:domain-definition:grouped-measure:v1）。"
                ]
            },
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="metric.grouped_measure 怎么定义？",
    )

    artifact_event = next(payload for name, payload in events if name == "artifact")
    assert artifact_event["type"] == "citations"
    artifact = store.list_artifacts(conversation.id)[0]
    assert artifact.source_tool == "domain_definition_lookup"
    run_id = cast(str, events[-1][1]["run_id"])
    evidence = TaskStore(store.db_path).list_evidence(run_id)
    assert len(evidence) == 1
    assert evidence[0].source["tool"] == "domain_definition_lookup"


@pytest.mark.asyncio
async def test_compiled_definition_binds_data_evidence_and_numeric_claim(
    store: SessionStore,
    conversation: Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_inline(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_loop_module, "run_in_threadpool", run_inline)
    _register_dataset(store, conversation)
    definition_id = "a" * 32
    formula_hash = "b" * 64
    source_ref = "urn:domain-definition:grouped-measure:v1"
    compiled_arguments = {
        "dataset_ref": _DATASET_REF,
        "group_col": "bucket_code",
        "agg": "sum",
        "value_col": "measure_value",
        "sort": "group",
        "limit": 100,
    }
    definition_result = {
        "status": "resolved",
        "is_empty": False,
        "requires_clarification": False,
        "semantic_key": "metric.grouped_measure",
        "as_of": "2026-06-01T00:00:00Z",
        "definition": {
            "definition_id": definition_id,
            "version": 1,
            "title": "匿名分组度量",
            "description": "按匿名分组汇总匿名度量。",
            "formula_hash": formula_hash,
            "resource_uri": f"chatbi://domain-definitions/{definition_id}",
            "source": source_ref,
        },
        "candidates": [],
        "compilation_status": "ready",
        "compiled_invocation": {
            "definition_id": definition_id,
            "definition_version": 1,
            "formula_hash": formula_hash,
            "tool_name": "aggregate_preview",
            "arguments": compiled_arguments,
        },
    }
    registry = FakeRegistry(
        {
            "domain_definition_lookup": lambda _args: definition_result,
            "aggregate_preview": lambda _args: {
                "rows": [{"bucket_code": "A", "value": 25.0}],
                "group_total": 1,
                "agg": "sum",
            },
        }
    )
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="definition-call",
                        name="domain_definition_lookup",
                        arguments=json.dumps(
                            {
                                "semantic_key": "metric.grouped_measure",
                                "dataset_ref": _DATASET_REF,
                            }
                        ),
                    )
                ]
            },
            {
                "tool_calls": [
                    ToolCall(
                        id="compiled-data-call",
                        name="aggregate_preview",
                        arguments=json.dumps(compiled_arguments),
                    )
                ]
            },
            {"deltas": [f"受控汇总值为 25（来源：{source_ref}）。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="按 metric.grouped_measure 汇总数据",
    )

    run_id = cast(str, dict(events)["done"]["run_id"])
    tasks = TaskStore(store.db_path)
    evidence = tasks.list_evidence(run_id)
    definition_evidence = next(
        item for item in evidence if item.source["tool"] == "domain_definition_lookup"
    )
    data_evidence = next(
        item for item in evidence if item.source["tool"] == "aggregate_preview"
    )
    assert definition_evidence.source["definition_resource"] == {
        "definition_id": definition_id,
        "definition_version": 1,
        "semantic_key": "metric.grouped_measure",
        "formula_hash": formula_hash,
        "resource_uri": f"chatbi://domain-definitions/{definition_id}",
        "source_ref": source_ref,
    }
    assert data_evidence.source["definition_execution"] == {
        **definition_evidence.source["definition_resource"],
        "definition_evidence_id": definition_evidence.evidence_id,
        "compiled_tool_name": "aggregate_preview",
        "compiled_arguments_hash": hashlib.sha256(
            json.dumps(
                compiled_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    numeric_claim = next(
        claim for claim in tasks.list_claims(run_id) if claim.claim_kind == "numeric"
    )
    assert set(numeric_claim.evidence_ids) == {
        definition_evidence.evidence_id,
        data_evidence.evidence_id,
    }
    assert any(
        ref.get("kind") == "definition_execution"
        and ref.get("supported") is True
        for ref in numeric_claim.value_refs
    )


@pytest.mark.asyncio
async def test_kb_answer_without_source_is_revised_before_delivery(
    store: SessionStore, conversation: Conversation
) -> None:
    result = {
        "is_empty": False,
        "hits": [
            {
                "source": "指标口径.md",
                "section": "活跃用户",
                "text": "活跃用户指统计周期内有效登录的去重用户数。",
            }
        ],
    }
    registry = FakeRegistry({"kb_search": lambda args: result})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="kb-call",
                        name="kb_search",
                        arguments='{"query":"活跃用户怎么定义"}',
                    )
                ]
            },
            {"deltas": ["活跃用户是有效登录的去重用户数。"]},
            {"deltas": ["活跃用户是有效登录的去重用户数（来源：指标口径.md）。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="活跃用户怎么定义？",
    )

    verification_events = [payload for name, payload in events if name == "verification"]
    assert verification_events[0]["payload"]["checks"][0]["code"] == ("unsupported_knowledge_claim")
    assert verification_events[-1]["payload"]["verdict"] == "PASS"
    assert dict(events)["done"]["run_status"] == "completed"
    assert store.list_messages(conversation.id)[-1].content.endswith("指标口径.md）。")


@pytest.mark.asyncio
async def test_explicit_chart_request_cannot_finish_before_chart_artifact(
    store: SessionStore, conversation: Conversation
) -> None:
    """明确要图时，模型提前文字收尾会被纠正，成功出图后才能结束。"""
    _register_dataset(store, conversation)
    chart_result = {
        "chart_id": "chart-1",
        "chart_type": "line",
        "option": {"xAxis": {"data": ["1月"]}, "series": [{"data": [10]}]},
    }
    registry = FakeRegistry({"gen_chart": lambda args: chart_result})
    gateway = ScriptedGateway(
        [
            {"deltas": ["分析已经完成，销售额呈上升趋势。"]},
            {
                "deltas": ["我补充生成趋势图。"],
                "tool_calls": [
                    ToolCall(
                        id="chart-call",
                        name="gen_chart",
                        arguments=(
                            f'{{"dataset_ref":"{_DATASET_REF}","chart_type":"line",'
                            '"encoding":{"x":"月份","y":"销售额","agg":"sum"}}'
                        ),
                    )
                ],
            },
            {"deltas": ["趋势图已生成。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="请分析销售趋势并生成折线图",
    )

    assert [name for name, _ in events].count("artifact") == 1
    assert next(payload for name, payload in events if name == "artifact")["type"] == "chart"
    streamed = "".join(payload["delta"] for name, payload in events if name == "text.delta")
    assert "分析已经完成" not in streamed  # 未完成的提前答复没有泄漏到前端
    assert "趋势图已生成" in streamed
    assert [name for name, _ in events][-1] == "done"
    assert registry.executed[0][0] == "gen_chart"
    assert len(gateway.calls) == 3
    correction = gateway.calls[1]["messages"][-1]
    assert correction.role == "user" and "请先调用 gen_chart" in correction.content


@pytest.mark.asyncio
async def test_explicit_chart_request_errors_if_retry_still_returns_only_text(
    store: SessionStore, conversation: Conversation
) -> None:
    """纠正一次仍不调用图表工具时明确失败，不保存“已完成”的假最终答复。"""
    _register_dataset(store, conversation)
    gateway = ScriptedGateway(
        [
            {"deltas": ["第一次只给文字。"]},
            {"deltas": ["第二次仍然只给文字。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({"gen_chart": lambda args: {}}),
        user_text="把销售额做成图表",
    )

    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "run.started",
        "verification.started",
        "verification",
        "verification.started",
        "verification",
        "error",
    ]
    assert events[-1][1]["code"] == "chart_not_generated"
    assert [message.role for message in store.list_messages(conversation.id)] == ["user"]


@pytest.mark.asyncio
async def test_explicit_pdf_report_cannot_finish_before_report_artifact(
    store: SessionStore, conversation: Conversation, tmp_path: Path
) -> None:
    """明确要 PDF 报告时，必须落库并发送带 pdf_url 的 report Artifact。"""
    markdown_path = tmp_path / f"{_REPORT_ID}.md"
    pdf_path = tmp_path / f"{_REPORT_ID}.pdf"
    markdown_path.write_text("# 销售分析报告", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.7\n")
    report_result = {
        "report_id": _REPORT_ID,
        "md_path": str(markdown_path),
        "pdf_path": str(pdf_path),
        "skipped_charts": 0,
        "analysis_ids": ["analysis-1"],
    }
    registry = FakeRegistry({"generate_report": lambda args: report_result})
    gateway = ScriptedGateway(
        [
            {"deltas": ["PDF 已生成，可下载。"]},
            {
                "deltas": ["我现在生成报告文件。"],
                "tool_calls": [
                    ToolCall(
                        id="report-call",
                        name="generate_report",
                        arguments=(
                            '{"title":"销售分析报告","analysis_ids":["analysis-1"],'
                            '"include_pdf":true}'
                        ),
                    )
                ],
            },
            {"deltas": ["报告和 PDF 已生成。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="请把已完成的分析组装成报告，并导出 PDF",
    )

    artifact_event = next(payload for name, payload in events if name == "artifact")
    assert artifact_event["type"] == "report"
    assert artifact_event["payload"]["report_id"] == _REPORT_ID
    assert artifact_event["payload"]["pdf_url"] == (f"/analyze/report/{_REPORT_ID}.pdf")
    artifacts = store.list_artifacts(conversation.id)
    assert len(artifacts) == 1 and artifacts[0].id == artifact_event["id"]
    assert artifacts[0].file_ref == str(pdf_path)
    assert store.report_project_id(_REPORT_ID) == conversation.project_id
    streamed = "".join(payload["delta"] for name, payload in events if name == "text.delta")
    assert "PDF 已生成，可下载" not in streamed
    assert "报告和 PDF 已生成" in streamed
    assert events[-1][0] == "done"
    correction = gateway.calls[1]["messages"][-1]
    assert correction.role == "user"
    assert "include_pdf 设为 true" in correction.content


@pytest.mark.asyncio
async def test_explicit_report_errors_if_retry_still_returns_only_text(
    store: SessionStore, conversation: Conversation
) -> None:
    """纠正一次仍不调用报告工具时明确失败，不保存虚假的成功答复。"""
    gateway = ScriptedGateway(
        [
            {"deltas": ["报告已生成。"]},
            {"deltas": ["PDF 已生成，可下载。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({"generate_report": lambda args: {}}),
        user_text="请生成一份分析报告",
    )

    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "run.started",
        "verification.started",
        "verification",
        "verification.started",
        "verification",
        "error",
    ]
    assert events[-1][1]["code"] == "report_not_generated"
    assert [message.role for message in store.list_messages(conversation.id)] == ["user"]


@pytest.mark.asyncio
async def test_report_tool_result_without_real_file_cannot_create_artifact(
    store: SessionStore, conversation: Conversation, tmp_path: Path
) -> None:
    missing_pdf = tmp_path / "missing.pdf"
    registry = FakeRegistry(
        {
            "generate_report": lambda args: {
                "report_id": "missing-report",
                "md_path": str(tmp_path / "missing.md"),
                "pdf_path": str(missing_pdf),
            }
        }
    )
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["我来生成报告。"],
                "tool_calls": [
                    ToolCall(
                        id="report-missing-file",
                        name="generate_report",
                        arguments='{"title":"报告","analysis_ids":[],"include_pdf":true}',
                    )
                ],
            },
            {"deltas": ["PDF 已生成。"]},
            {"deltas": ["仍然完成了。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="请导出 PDF 报告",
    )

    assert not any(name == "artifact" for name, _ in events)
    tool_end = next(payload for name, payload in events if name == "tool_end")
    assert tool_end["status"] == "error"
    assert "真实报告文件" in tool_end["message"]
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "report_not_generated"
    assert store.list_artifacts(conversation.id) == []


@pytest.mark.asyncio
async def test_report_files_are_cleaned_if_atomic_success_commit_fails(
    store: SessionStore,
    conversation: Conversation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_id = "commit-failure-report"
    markdown_path = tmp_path / f"{report_id}.md"
    pdf_path = tmp_path / f"{report_id}.pdf"
    markdown_path.write_text("# 待提交报告", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(
        "apps.orchestrator.agent_loop.get_settings",
        lambda: type("SettingsStub", (), {"report_dir": str(tmp_path)})(),
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_agent_evidence
            BEFORE INSERT ON evidence
            BEGIN
                SELECT RAISE(ABORT, 'forced agent evidence failure');
            END
            """
        )
    registry = FakeRegistry(
        {
            "generate_report": lambda args: {
                "report_id": report_id,
                "md_path": str(markdown_path),
                "pdf_path": str(pdf_path),
                "analysis_ids": ["analysis-1"],
            }
        }
    )
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="report-commit-failure",
                        name="generate_report",
                        arguments=(
                            '{"title":"报告","analysis_ids":["analysis-1"],' '"include_pdf":true}'
                        ),
                    )
                ]
            }
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="请生成 PDF 报告",
    )

    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "persistence_failed"
    assert store.list_artifacts(conversation.id) == []
    assert not markdown_path.exists()
    assert not pdf_path.exists()
    run_id = cast(str, events[-1][1]["run_id"])
    assert TaskStore(store.db_path).list_evidence(run_id) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("请生成销售折线图", True),
        ("visualize sales by month", True),
        ("分析销售额的变化并给出文字结论", False),
        ("不要图表，只给文字结论", False),
    ],
)
def test_chart_intent_detection_is_conservative(text: str, expected: bool) -> None:
    from apps.orchestrator.agent_loop import _requests_chart

    assert _requests_chart(text) is expected


@pytest.mark.parametrize(
    ("text", "expected", "pdf_expected"),
    [
        ("请把本次分析组装成一份报告，并导出 PDF", True, True),
        ("给我一份销售分析报告", True, False),
        ("报告通常包含哪些内容？", False, False),
        ("不要生成报告，只给文字结论", False, False),
        ("生成报告，但不要 PDF", True, False),
    ],
)
def test_report_intent_detection_is_conservative(
    text: str, expected: bool, pdf_expected: bool
) -> None:
    from apps.orchestrator.agent_loop import _requests_pdf, _requests_report

    assert _requests_report(text) is expected
    assert (_requests_report(text) and _requests_pdf(text)) is pdf_expected


@pytest.mark.asyncio
async def test_tool_failure_feeds_error_back_for_retry(
    store: SessionStore, conversation: Conversation
) -> None:
    """校验/业务失败回传模型带错重试（14.5.1，复用 analyze 已验证模式）。"""
    attempts = {"n": 0}

    def flaky(args: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise SchemaValidationError("入参校验失败 @ value_col: 缺少必填字段")
        return {"rows": [], "group_total": 0, "agg": "sum"}

    registry = FakeRegistry({"aggregate_preview": flaky})
    call = {"tool_calls": [ToolCall(id="c1", name="aggregate_preview", arguments="{}")]}
    retry = {
        "tool_calls": [
            ToolCall(id="c2", name="aggregate_preview", arguments='{"value_col":"销售额"}')
        ]
    }
    gateway = ScriptedGateway([call, retry, {"deltas": ["修好了"]}])

    events = await _run_loop(store, conversation, gateway, registry)

    ends = [payload for name, payload in events if name == "tool_end"]
    assert [end["status"] for end in ends] == ["error", "ok"]
    assert "入参校验失败" in ends[0]["message"]
    assert dict(events)["done"]["tool_calls"] == 2
    # 失败结果以 tool 消息回传模型
    second_call = gateway.calls[1]["messages"]
    assert any(m.role == "tool" and "工具执行失败" in m.content for m in second_call)


@pytest.mark.asyncio
async def test_policy_denial_is_persisted_before_tool_execution(
    store: SessionStore, conversation: Conversation
) -> None:
    registry = FakeRegistry({"code_interpreter": lambda args: pytest.fail("被拒工具不得执行")})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="unsafe-call",
                        name="code_interpreter",
                        arguments='{"code":"print(1)"}',
                    )
                ]
            },
            {"deltas": ["该能力未获准，因此没有执行代码。"]},
        ]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    assert registry.executed == []
    started = next(payload for name, payload in events if name == "step.started")
    assert started["payload"]["policy"]["code"] == "tool_not_allowlisted"
    completed = next(payload for name, payload in events if name == "step.completed")
    assert completed["payload"]["status"] == "failed"
    assert completed["payload"]["observation"]["source"] == "policy"
    assert completed["payload"]["observation"]["code"] == "tool_not_allowlisted"
    assert dict(events)["done"]["tool_calls"] == 0
    assert dict(events)["done"]["tool_attempts"] == 1
    assert dict(events)["done"]["invalid_tool_calls"] == 1


@pytest.mark.asyncio
async def test_policy_denials_consume_budget_and_disable_tools(
    store: SessionStore, conversation: Conversation
) -> None:
    registry = FakeRegistry({"code_interpreter": lambda args: pytest.fail("被拒工具不得执行")})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="denied-once",
                        name="code_interpreter",
                        arguments='{"code":"print(1)"}',
                    )
                ]
            },
            {"deltas": ["该能力没有执行。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        config=AgentLoopConfig(max_tool_calls=1, tool_result_max_chars=500),
    )

    assert registry.executed == []
    assert gateway.calls[1]["tools"] is None
    done = dict(events)["done"]
    assert done["tool_calls"] == 0
    assert done["tool_attempts"] == 1
    # 拒绝已被如实说明，且本任务没有未完成的 Required criterion，可以安全完成。
    assert done["run_status"] == "completed"


@pytest.mark.asyncio
async def test_invalid_tool_call_limit_stops_varying_hallucinated_calls(
    store: SessionStore, conversation: Conversation
) -> None:
    registry = FakeRegistry({"code_interpreter": lambda args: pytest.fail("被拒工具不得执行")})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id=f"hallucinated-{index}",
                        name="code_interpreter",
                        arguments=json.dumps({"code": f"print({index})"}),
                    )
                ]
            }
            for index in range(3)
        ]
        + [{"deltas": ["无法执行未授权能力。"]}]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    assert registry.executed == []
    assert len(gateway.calls) == 4
    assert gateway.calls[-1]["tools"] is None
    tool_ends = [payload for name, payload in events if name == "tool_end"]
    assert len(tool_ends) == 3
    assert "无效工具调用次数已达上限" in tool_ends[-1]["message"]


@pytest.mark.asyncio
async def test_consecutive_same_tool_same_args_circuit_breaks(
    store: SessionStore, conversation: Conversation
) -> None:
    """连续两次同工具同参数 → 熔断，随后禁用 tools 强制作答（14.5.1）。"""
    registry = FakeRegistry({"kb_search": lambda args: {"is_empty": True, "hits": []}})
    same = {"tool_calls": [ToolCall(id="c1", name="kb_search", arguments='{"query":"口径"}')]}
    same2 = {"tool_calls": [ToolCall(id="c2", name="kb_search", arguments='{"query":"口径"}')]}
    gateway = ScriptedGateway([same, same2, {"deltas": ["我没能检索到更多结果"]}])

    events = await _run_loop(store, conversation, gateway, registry)

    ends = [payload for name, payload in events if name == "tool_end"]
    assert [end["status"] for end in ends] == ["ok", "error"]
    assert "熔断" in ends[1]["message"]
    assert len(registry.executed) == 1  # 第二次没有真正执行
    assert gateway.calls[2]["tools"] is None  # 熔断后禁用 tools 强制作答
    # 每步结果都有回放记录（含未执行的熔断步）
    outcomes = [
        json.loads(m.content) for m in store.list_messages(conversation.id) if m.role == "tool"
    ]
    assert [(o["tool_call_id"], o["status"]) for o in outcomes] == [
        ("c1", "ok"),
        ("c2", "error"),
    ]


@pytest.mark.asyncio
async def test_unbound_plan_calls_use_stable_signature_across_random_call_ids(
    store: SessionStore, conversation: Conversation
) -> None:
    registry = FakeRegistry(
        {
            "get_data_profile": lambda args: {"profile": {}},
            "aggregate_preview": lambda args: pytest.fail("计划外工具不得执行"),
        }
    )
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="random-id-1",
                        name="aggregate_preview",
                        arguments='{"value_col":"销售额"}',
                    )
                ]
            },
            {
                "tool_calls": [
                    ToolCall(
                        id="random-id-2",
                        name="aggregate_preview",
                        arguments='{"value_col":"销售额"}',
                    )
                ]
            },
            {"deltas": ["没有执行计划外能力。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="介绍这份数据的规模和质量",
        enforce_plan=True,
    )

    ends = [payload for name, payload in events if name == "tool_end"]
    assert "不属于当前持久化计划" in ends[0]["message"]
    assert "熔断" in ends[1]["message"]
    assert registry.executed == []
    assert gateway.calls[2]["tools"] is None


@pytest.mark.asyncio
async def test_model_round_limit_stops_nonconverging_executor(
    store: SessionStore, conversation: Conversation
) -> None:
    registry = FakeRegistry({"code_interpreter": lambda args: pytest.fail("被拒工具不得执行")})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id=f"round-{index}",
                        name="code_interpreter",
                        arguments=json.dumps({"code": f"print({index})"}),
                    )
                ]
            }
            for index in range(3)
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        config=AgentLoopConfig(
            max_invalid_tool_calls=10,
            max_model_rounds=2,
            tool_result_max_chars=500,
        ),
    )

    assert len(gateway.calls) == 2
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "model_round_limit_exhausted"


@pytest.mark.asyncio
async def test_tool_call_budget_is_enforced(
    store: SessionStore, conversation: Conversation
) -> None:
    """单轮工具调用总数 ≤ max_tool_calls；超出的调用不执行并回传上限提示。"""
    registry = FakeRegistry({"kb_search": lambda args: {"is_empty": True, "hits": []}})
    burst = {
        "tool_calls": [
            ToolCall(id="c1", name="kb_search", arguments='{"query":"a"}'),
            ToolCall(id="c2", name="kb_search", arguments='{"query":"b"}'),
        ]
    }
    gateway = ScriptedGateway([burst, {"deltas": ["就查到这些"]}])
    config = AgentLoopConfig(max_tool_calls=1, tool_result_max_chars=500)

    events = await _run_loop(store, conversation, gateway, registry, config=config)

    ends = [payload for name, payload in events if name == "tool_end"]
    assert [end["status"] for end in ends] == ["ok", "error"]
    assert "上限" in ends[1]["message"]
    assert len(registry.executed) == 1
    assert gateway.calls[1]["tools"] is None
    assert dict(events)["done"]["tool_calls"] == 1
    assert dict(events)["done"]["run_status"] == "blocked"


def test_host_enriches_report_delivery_and_referenced_chart_lineage(
    store: SessionStore, conversation: Conversation
) -> None:
    first_ref = "1" * 32
    second_ref = "2" * 32
    _register_dataset(store, conversation, first_ref)
    _register_dataset(store, conversation, second_ref)
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已有分析",
    )
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="stats",
        payload={"result": {"direction": "up"}},
        source_tool="trend_analysis",
        params={"analysis_id": "stats-analysis"},
        dataset_ref=first_ref,
    )
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="chart",
        payload={"chart_type": "line"},
        source_tool="gen_chart",
        params={"analysis_id": "chart-analysis-1", "grain": "day"},
        dataset_ref=first_ref,
    )
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="chart",
        payload={"chart_type": "line"},
        source_tool="gen_chart",
        params={"analysis_id": "chart-analysis-2", "grain": "week"},
        dataset_ref=second_ref,
    )
    artifacts = store.list_artifacts(conversation.id)
    datasets = store.list_datasets(conversation.project_id)

    report_contract = build_minimal_contract(
        run_id="report-enrichment",
        user_text="把刚才的趋势和图表生成 PDF 报告。",
        chart_required=False,
        report_required=True,
        pdf_required=True,
    )
    report_args = _enrich_tool_arguments(
        "generate_report",
        {},
        contract=report_contract,
        artifacts=artifacts,
        datasets=datasets,
    )
    assert report_args == {
        "analysis_ids": [
            "stats-analysis",
            "chart-analysis-1",
            "chart-analysis-2",
        ],
        "include_pdf": True,
        "title": "数据分析报告",
    }

    chart_contract = build_minimal_contract(
        run_id="chart-enrichment",
        user_text="把第二张图改成按月展示。",
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )
    chart_args = _enrich_tool_arguments(
        "gen_chart",
        {"chart_type": "line"},
        contract=chart_contract,
        artifacts=artifacts,
        datasets=datasets,
    )
    assert chart_args["dataset_ref"] == second_ref


def test_replanner_cannot_drop_or_replace_host_reference_assumptions() -> None:
    previous = {
        "assumptions": [
            "ordinary-old",
            'HOST_COREF_V1:{"fixed":true}',
            'HOST_MEMORY_REF_V1:{"fixed":true}',
        ]
    }
    revised = {
        "schema_version": 1,
        "summary": "修订计划",
        "steps": [],
        "assumptions": [
            "ordinary-new",
            'HOST_COREF_V1:{"forged":true}',
            'HOST_MEMORY_REF_V1:{"forged":true}',
        ],
        "clarifications": [],
    }

    result = _preserve_host_reference_assumptions(revised, previous)

    assert result["assumptions"] == [
        "ordinary-new",
        'HOST_COREF_V1:{"fixed":true}',
        'HOST_MEMORY_REF_V1:{"fixed":true}',
    ]


def test_verified_reference_overrides_model_delivery_lineage(
    store: SessionStore, conversation: Conversation
) -> None:
    first_ref = "1" * 32
    second_ref = "2" * 32
    _register_dataset(store, conversation, first_ref)
    _register_dataset(store, conversation, second_ref)
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已有分析",
    )
    stats = store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="stats",
        payload={"result": {"direction": "up"}},
        source_tool="trend_analysis",
        params={"analysis_id": "stats-analysis"},
        dataset_ref=first_ref,
    )
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="chart",
        payload={"chart_type": "line"},
        source_tool="gen_chart",
        params={"analysis_id": "chart-analysis-1"},
        dataset_ref=first_ref,
    )
    second_chart = store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="chart",
        payload={"chart_type": "bar"},
        source_tool="gen_chart",
        params={"analysis_id": "chart-analysis-2"},
        dataset_ref=second_ref,
    )
    artifacts = store.list_artifacts(conversation.id)
    datasets = store.list_datasets(conversation.project_id)
    resolver = ReferenceResolver(store, audit_recorder=lambda _event: None)

    chart_reference = resolver.resolve(
        "把第二张图改成按月展示",
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        principal=Principal(user_id="local-user"),
    )
    chart_contract = build_minimal_contract(
        run_id="verified-chart-lineage",
        user_text=chart_reference.rewritten_query,
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )
    chart_args = _enrich_tool_arguments(
        "gen_chart",
        {"chart_type": "line", "dataset_ref": first_ref},
        contract=chart_contract,
        artifacts=artifacts,
        datasets=datasets,
        references=chart_reference,
    )
    assert chart_args["dataset_ref"] == second_ref

    report_reference = resolver.resolve(
        "把刚才的趋势和图表生成报告",
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        principal=Principal(user_id="local-user"),
    )
    report_contract = build_minimal_contract(
        run_id="verified-report-lineage",
        user_text=report_reference.rewritten_query,
        chart_required=False,
        report_required=True,
        pdf_required=False,
    )
    report_args = _enrich_tool_arguments(
        "generate_report",
        {"analysis_ids": ["model-selected-wrong-id"]},
        contract=report_contract,
        artifacts=artifacts,
        datasets=datasets,
        references=report_reference,
    )
    assert report_args["analysis_ids"] == [
        cast(str, stats.params["analysis_id"]),
        cast(str, second_chart.params["analysis_id"]),
    ]


@pytest.mark.asyncio
async def test_ambiguous_artifact_reference_waits_without_calling_model(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    first_ref = "1" * 32
    second_ref = "2" * 32
    _register_dataset(store, conversation, first_ref)
    _register_dataset(store, conversation, second_ref)
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已有两张图",
    )
    for dataset_ref in (first_ref, second_ref):
        store.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="chart",
            payload={"chart_type": "line"},
            source_tool="gen_chart",
            params={},
            dataset_ref=dataset_ref,
        )
    gateway = ScriptedGateway([{"deltas": ["不应调用模型"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="把这个图改成柱状图",
    )

    assert gateway.calls == []
    assert dict(events)["meta"]["reference_status"] == "ambiguous"
    waiting = dict(events)["waiting_user"]
    assert waiting["payload"]["about"] == "reference_target"
    assert "多个可能目标" in dict(events)["text.delta"]["delta"]
    assert dict(events)["done"]["run_status"] == "waiting_user"


@pytest.mark.asyncio
async def test_resolved_reference_plan_binding_survives_newer_artifact(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    dataset_ref = "1" * 32
    _register_dataset(store, conversation, dataset_ref)
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已有两张图",
    )
    charts = [
        store.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="chart",
            payload={"chart_type": chart_type},
            source_tool="gen_chart",
            params={},
            dataset_ref=dataset_ref,
        )
        for chart_type in ("line", "bar")
    ]
    gateway = ScriptedGateway([{"deltas": ["已识别目标。"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="总结第二张图",
    )

    done = dict(events)["done"]
    assert done["run_status"] == "completed"
    tasks = TaskStore(store.db_path)
    plan_record = tasks.get_active_plan(cast(str, done["run_id"]))
    assert plan_record is not None
    assumption = find_reference_assumption(plan_record.plan.get("assumptions", []))
    assert assumption is not None
    assert len(assumption) <= 300
    validation = validate_task_plan(
        plan_record.plan,
        capabilities=set(),
    )
    assert validation.schema_valid is True

    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="chart",
        payload={"chart_type": "scatter"},
        source_tool="gen_chart",
        params={},
        dataset_ref=dataset_ref,
    )
    restored = ReferenceResolver(
        store,
        audit_recorder=lambda _event: None,
    ).restore(
        assumption,
        query="恢复任务",
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        principal=Principal(user_id="local-user"),
    )
    assert [target.reference_id for target in restored.targets] == [charts[1].id]


@pytest.mark.asyncio
async def test_governed_memory_reference_binds_plan_and_chart_dataset(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    first_ref = "1" * 32
    second_ref = "2" * 32
    _register_dataset(store, conversation, first_ref)
    _register_dataset(store, conversation, second_ref)
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="主数据",
        target_ref=second_ref,
        key="primary",
    )
    registry = FakeRegistry(
        {
            "gen_chart": lambda _args: {
                "chart_id": "memory-chart",
                "chart_type": "line",
                "option": {"xAxis": {"data": []}, "series": []},
            }
        }
    )
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["生成图表。"],
                "tool_calls": [
                    ToolCall(
                        id="memory-chart-call",
                        name="gen_chart",
                        arguments=(
                            f'{{"dataset_ref":"{first_ref}",' '"chart_type":"line","encoding":{}}'
                        ),
                    )
                ],
            },
            {"deltas": ["图表已生成。"]},
        ]
    )

    events = await _run_loop(
        store,
        conversation,
        gateway,
        registry,
        user_text="使用主数据生成折线图",
    )

    meta = dict(events)["meta"]
    assert meta["memory_reference_status"] == "resolved"
    assert registry.executed, (
        [name for name, _ in events],
        events[-1],
    )
    assert json.loads(registry.executed[0][1])["dataset_ref"] == second_ref
    plan = TaskStore(store.db_path).get_active_plan(cast(str, meta["run_id"]))
    assert plan is not None
    assumptions = find_memory_reference_assumptions(plan.plan.get("assumptions", []))
    assert len(assumptions) == 1
    assert memory_id in assumptions[0]
    assert len(assumptions[0]) <= 300
    assert validate_task_plan(
        plan.plan,
        capabilities={"visualization.chart"},
    ).schema_valid
    assert memory_id in gateway.calls[0]["messages"][0].content
    assert dict(events)["done"]["run_status"] == "completed"


@pytest.mark.asyncio
async def test_ambiguous_memory_reference_waits_without_model_or_write(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    first_ref = "1" * 32
    second_ref = "2" * 32
    _register_dataset(store, conversation, first_ref)
    _register_dataset(store, conversation, second_ref)
    _remember_agent_reference(
        store,
        conversation,
        alias="当前批次",
        target_ref=first_ref,
        key="project-scope",
    )
    _remember_agent_reference(
        store,
        conversation,
        alias="当前批次",
        target_ref=second_ref,
        key="conversation-scope",
        scope="conversation",
    )
    memories = MemoryStore(store)
    before = memories.list_records_for_governance(
        project_id=conversation.project_id,
        principal=Principal(user_id="local-user"),
        conversation_id=conversation.id,
    )
    gateway = ScriptedGateway([{"deltas": ["不应调用模型"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="总结当前批次",
    )

    assert gateway.calls == []
    assert dict(events)["meta"]["memory_reference_status"] == "ambiguous"
    waiting = dict(events)["waiting_user"]
    assert waiting["payload"]["about"] == "memory_reference_target"
    assert "多个受治理映射" in dict(events)["text.delta"]["delta"]
    after = memories.list_records_for_governance(
        project_id=conversation.project_id,
        principal=Principal(user_id="local-user"),
        conversation_id=conversation.id,
    )
    assert [(item.memory_id, item.status) for item in after] == [
        (item.memory_id, item.status) for item in before
    ]


@pytest.mark.asyncio
async def test_field_alias_is_injected_as_verified_host_context(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    dataset_ref = "1" * 32
    _register_dataset(store, conversation, dataset_ref)
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="请求 ID",
        target_ref=dataset_ref,
        key="field-alias",
        kind="field_alias",
        canonical_field="工单编号",
    )
    gateway = ScriptedGateway([{"deltas": ["已确认字段。"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="请求 ID 对应哪个字段",
    )

    assert dict(events)["meta"]["memory_reference_status"] == "resolved"
    system = gateway.calls[0]["messages"][0].content
    assert memory_id in system
    assert '"canonical_field":"工单编号"' in system
    assert dict(events)["done"]["run_status"] == "completed"


@pytest.mark.asyncio
async def test_system_context_lists_datasets_and_analysis_registry(
    store: SessionStore, conversation: Conversation
) -> None:
    """上下文装配：数据集清单（含血缘）+ 最新画像 + 分析登记表（14.5.2）。"""
    _register_dataset(store, conversation, "d1")
    store.register_dataset(
        ref="d2",
        project_id=conversation.project_id,
        filename="销售.xlsx（衍生）",
        profile={"row_count": 2, "column_count": 2, "columns": []},
        parent_ref="d1",
        transform={"drop_nulls": []},
    )
    seed = store.append_message(conversation_id=conversation.id, role="assistant", content="旧分析")
    artifact = store.create_artifact(
        conversation_id=conversation.id,
        message_id=seed.id,
        type="stats",
        payload={"kind": "trend_analysis", "result": {"direction": "up"}},
        source_tool="trend_analysis",
        params={"analysis_id": "an-001", "time_col": "月份"},
        dataset_ref="d1",
    )
    del artifact
    gateway = ScriptedGateway([{"deltas": ["好的"]}])

    await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="继续使用 d2",
    )

    system = gateway.calls[0]["messages"][0]
    assert system.role == "system"
    assert "可用数据集" in system.content
    assert "d1" in system.content and "d2" in system.content
    assert "衍生自 d1" in system.content
    assert '"row_count":2' in system.content  # 最新数据集画像
    assert "分析登记表" in system.content
    assert "analysis_id=an-001" in system.content
    assert '"direction":"up"' in system.content  # 登记表摘要


@pytest.mark.asyncio
async def test_system_context_uses_fixed_governed_memory_snapshot(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    """Agent 只读取当前 TaskRun 快照摘要，并明确禁止把记忆当 Evidence。"""
    source = store.append_message(
        conversation_id=conversation.id,
        role="user",
        content="以后将工单编号称为请求 ID",
    )
    memory = (
        MemoryStore(store)
        .remember(
            project_id=conversation.project_id,
            principal=Principal(user_id="local-user"),
            draft=MemoryDraft(
                scope="project",
                kind="field_alias",
                semantic_key="field-alias.ticket-id",
                content_summary="工单编号的展示名称是请求 ID",
                source_type="user_confirmation",
                source_ref=source.id,
                source_hash=hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
                confidence=0.95,
            ),
            idempotency_key="agent-memory-context",
        )
        .record
    )
    gateway = ScriptedGateway([{"deltas": ["好的"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="继续",
    )

    system = gateway.calls[0]["messages"][0]
    assert "受控记忆" in system.content
    assert "不能作为数值、统计、工件或知识来源 Evidence" in system.content
    assert memory.content_summary in system.content
    assert source.content not in system.content
    meta = dict(events)["meta"]
    restored = MemoryStore(store).get_snapshot(
        cast(str, meta["memory_snapshot_id"]),
        principal=Principal(user_id="local-user"),
    )
    assert restored is not None
    assert [item.memory_id for item in restored[1]] == [memory.memory_id]


@pytest.mark.asyncio
async def test_history_excludes_tool_call_preambles(
    store: SessionStore, conversation: Conversation
) -> None:
    """历史回放剔除带 tool_calls 的开场白与 tool 消息，防止“只说不调”污染模式。

    否则长对话后模型会模仿被压平的假示范，停止发起工具调用并声称“图表已生成”。
    """
    store.append_message(conversation_id=conversation.id, role="user", content="画个图")
    store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="好的，我先查看画像然后出图。",
        tool_calls=[{"id": "old1", "name": "gen_chart", "arguments": "{}"}],
    )
    store.append_message(
        conversation_id=conversation.id,
        role="tool",
        content='{"tool_call_id":"old1","tool":"gen_chart","status":"ok"}',
    )
    store.append_message(
        conversation_id=conversation.id, role="assistant", content="图表已生成，华东最高。"
    )
    gateway = ScriptedGateway([{"deltas": ["好的"]}])

    await _run_loop(store, conversation, gateway, FakeRegistry({}), user_text="继续")

    replayed = [(m.role, m.content) for m in gateway.calls[0]["messages"][1:]]
    assert replayed == [
        ("user", "画个图"),
        ("assistant", "图表已生成，华东最高。"),  # 只回放最终答复
        ("user", "继续"),
    ]


@pytest.mark.asyncio
async def test_long_history_uses_fixed_compaction_and_recent_raw_messages(
    store: SessionStore,
    conversation: Conversation,
) -> None:
    for index in range(3):
        store.append_message(
            conversation_id=conversation.id,
            role="user",
            content=f"历史问题 {index}：" + chr(0x7532 + index) * 70,
        )
        store.append_message(
            conversation_id=conversation.id,
            role="assistant",
            content=f"历史答复 {index}：" + chr(0x4E59 + index) * 70,
        )
    gateway = ScriptedGateway([{"deltas": ["完成"]}])

    events = await _run_loop(
        store,
        conversation,
        gateway,
        FakeRegistry({}),
        user_text="当前问题必须保持原文",
        config=AgentLoopConfig(
            history_limit=20,
            compaction_trigger_chars=100,
            compaction_keep_recent=2,
            compaction_summary_max_chars=800,
            tool_result_max_chars=500,
        ),
    )

    request = gateway.calls[0]["messages"]
    system = request[0].content
    assert "持久化历史上下文" in system
    assert "不能替代 Evidence、Artifact 或工具结果" in system
    assert "历史问题 0" in system
    assert [message.content for message in request[1:]] == [
        "历史答复 2：" + chr(0x4E59 + 2) * 70,
        "当前问题必须保持原文",
    ]
    meta = dict(events)["meta"]
    assert isinstance(meta["compaction_id"], str)
    snapshot_result = MemoryStore(store).get_snapshot(
        cast(str, meta["memory_snapshot_id"]),
        principal=Principal(user_id="local-user"),
    )
    assert snapshot_result is not None
    assert snapshot_result[0].compaction_id == meta["compaction_id"]


# ── 参数人话化（执行卡默认展示，替代原始 JSON）──


def test_humanize_args_translates_common_tools() -> None:
    from apps.orchestrator.agent_loop import _humanize_args

    assert (
        _humanize_args(
            "correlation",
            {"dataset_ref": "a1b2c3d4e5f6", "columns": ["销售额", "订单数"], "method": "pearson"},
        )
        == "数据集: a1b2c3d4 · 列: 销售额、订单数 · 方法: pearson"
    )

    assert (
        _humanize_args(
            "gen_chart",
            {
                "dataset_ref": "a1b2c3d4",
                "chart_type": "bar",
                "encoding": {"x": "地区", "y": "销售额", "agg": "sum"},
            },
        )
        == "数据集: a1b2c3d4 · 图型: bar · X轴: 地区 · Y轴: 销售额 · 聚合: sum"
    )

    assert (
        _humanize_args(
            "transform_dataset",
            {
                "dataset_ref": "a1b2c3d4",
                "filters": [{"column": "地区", "op": "in", "value": ["华东", "华南"]}],
                "sort": [{"column": "销售额", "order": "desc"}],
            },
        )
        == "数据集: a1b2c3d4 · 过滤: 地区 in 华东、华南 · 排序: 销售额 desc"
    )

    assert _humanize_args("chart_screenshot", {"option": {"series": []}}) == "渲染当前图表为 PNG"
    assert _humanize_args("kb_search", {}) == "无参数"


# ── /chat/stream 端点：SSE 协议与持久化（吸收原阶段1 用例）──


@dataclass
class ChatHarness:
    client: TestClient
    store: SessionStore
    gateway: ScriptedGateway
    conversation: Conversation
    requests: list[list[ModelMessage]] = field(default_factory=list)


@pytest.fixture
def chat_harness(tmp_path: Path) -> Iterator[ChatHarness]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("聊天项目")
    conversation = store.create_conversation(project.id)
    gateway = ScriptedGateway()
    app.dependency_overrides[session_store_dep] = lambda: store
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    app.dependency_overrides[settings_dep] = lambda: Settings(
        chat_db_path=str(tmp_path / "chatbi.db"),
        chat_history_limit=3,
        chat_profile_max_chars=2_000,
    )
    # sse-starlette 的进程级退出 Event 会绑定首次 TestClient 的事件循环；测试隔离时重置。
    AppStatus.should_exit_event = None
    try:
        with TestClient(app) as client:
            yield ChatHarness(client, store, gateway, conversation)
    finally:
        app.dependency_overrides.clear()
        AppStatus.should_exit_event = None


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    normalized = text.replace("\r\n", "\n").strip()
    for block in normalized.split("\n\n"):
        lines = block.splitlines()
        event = next(
            (line.split(": ", 1)[1] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line.split(": ", 1)[1] for line in lines if line.startswith("data: ")),
            None,
        )
        if event is not None and data is not None:
            events.append((event, cast(dict[str, Any], json.loads(data))))
    return events


def test_stream_chat_emits_protocol_and_persists_complete_reply(
    chat_harness: ChatHarness,
) -> None:
    response = chat_harness.client.post(
        "/chat/stream",
        json={
            "conversation_id": chat_harness.conversation.id,
            "message": "  请介绍一下系统能力  ",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "run.started",
        "verification.started",
        "verification",
        "text.delta",
        "text.delta",
        "done",
    ], events
    meta = events[0][1]
    done = events[-1][1]
    assert meta["autonomy_mode"] == "read_only"
    assert response.headers["x-chatbi-run-id"] == meta["run_id"]
    assert meta["conversation_id"] == chat_harness.conversation.id
    assert meta["message_id"] == done["message_id"]
    assert meta["run_id"] == done["run_id"]
    assert meta["title"] == "请介绍一下系统能力"
    assert done["characters"] == len("你好，有什么可以帮你？")
    assert done["tool_calls"] == 0

    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "请介绍一下系统能力"),
        ("assistant", "你好，有什么可以帮你？"),
    ]
    assert messages[1].id == meta["message_id"]
    call = chat_harness.gateway.calls[0]
    assert call["scenario"] == Scenario.AGENT
    assert call["tools"] is None, "无数据且计划无工具步骤时应走快速答复路径"
    run_response = chat_harness.client.get(f"/agent/runs/{meta['run_id']}")
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["plan"]["version"] == 1
    assert run_payload["plan"]["definition"]["steps"] == []
    assert run_payload["steps"] == []
    model_messages = call["messages"]
    assert model_messages[0].role == "system"
    assert "编造数字" in model_messages[0].content
    assert model_messages[-1].role == "user"
    assert model_messages[-1].content == "请介绍一下系统能力"


def test_resume_stream_reconstructs_lost_host_from_checkpoint(
    chat_harness: ChatHarness,
) -> None:
    for index in range(3):
        chat_harness.store.append_message(
            conversation_id=chat_harness.conversation.id,
            role="user",
            content=f"恢复前历史问题 {index}：" + "甲" * 70,
        )
        chat_harness.store.append_message(
            conversation_id=chat_harness.conversation.id,
            role="assistant",
            content=f"恢复前历史答复 {index}：" + "乙" * 70,
        )
    _, user_message = chat_harness.store.start_user_turn(
        conversation_id=chat_harness.conversation.id,
        content="继续完成已有任务",
        suggested_title="继续完成已有任务",
    )
    run_id = "checkpoint-recovery-run"
    contract = build_minimal_contract(
        run_id=run_id,
        user_text="继续完成已有任务",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    tasks = TaskStore(chat_harness.store.db_path)
    run, _ = tasks.create_run(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    compactions = CompactionStore(
        chat_harness.store,
        audit_recorder=lambda _event: None,
    )
    first_result = compactions.compact_if_needed(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        principal=Principal(user_id="local-user"),
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
    )
    assert first_result.view is not None
    fixed_compaction = first_result.view
    fixed_snapshot, _ = MemoryStore(chat_harness.store).create_snapshot(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        run_id=run.run_id,
        principal=Principal(user_id="local-user"),
        compaction_id=fixed_compaction.record.compaction_id,
    )
    newer_result = compactions.compact_if_needed(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        principal=Principal(user_id="local-user"),
        trigger_chars=100,
        keep_recent=1,
        summary_max_chars=800,
    )
    assert newer_result.view is not None
    assert newer_result.view.record.compaction_id != fixed_compaction.record.compaction_id
    run, _, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan={
            "schema_version": 1,
            "summary": "完成画像后形成最终答复",
            "steps": [
                {
                    "step_id": "profile",
                    "purpose": "读取已有画像",
                    "capability": "data.profile",
                    "dependencies": [],
                    "expected_evidence": ["画像 Evidence"],
                    "completion_conditions": ["画像读取完成"],
                    "fallback": [{"when": "失败", "action": "block"}],
                }
            ],
            "assumptions": [],
            "clarifications": [],
        },
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="completed-profile",
        tool_name="get_data_profile",
        arguments={"dataset_ref": _DATASET_REF},
        idempotency_key="completed-profile-before-restart",
        policy_decision={"allowed": True},
        step_id=steps[0].step_id,
    )
    tool_message = chat_harness.store.append_message(
        conversation_id=chat_harness.conversation.id,
        role="assistant",
        content="已读取画像",
    )
    run, _, _, _, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=tool_message.id,
        result={"profile": {"row_count": 3}},
        evidence_kind="tool_result",
        evidence_source={"tool": "get_data_profile"},
        evidence_summary={"summary": "画像读取完成"},
        artifact_draft=None,
    )
    paused, _, _ = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="pause-before-restart",
        command="pause",
        allowed_statuses={"running"},
        status="paused",
        event_type="run.paused",
        payload={"reason": "process_recovery"},
        require_idle=True,
        checkpoint_reason="process_recovery",
    )

    response = chat_harness.client.post(
        f"/agent/runs/{run.run_id}/resume/stream",
        headers={
            "Idempotency-Key": "resume-after-restart",
            "If-Match": str(paused.state_version),
        },
    )

    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert [name for name, _ in events] == [
        "run.resumed",
        "meta",
        "verification.started",
        "verification",
        "text.delta",
        "text.delta",
        "done",
    ], events
    assert events[1][1]["run_id"] == run.run_id
    assert events[1][1]["resumed"] is True
    assert events[1][1]["memory_snapshot_id"] == fixed_snapshot.memory_snapshot_id
    assert events[1][1]["compaction_id"] == fixed_compaction.record.compaction_id
    assert events[-1][1]["run_status"] == "completed"
    assert events[-1][1]["tool_calls"] == 1
    saved = tasks.get_run(run.run_id)
    assert saved is not None and saved.status == "completed"
    assert len(tasks.list_invocations(run.run_id)) == 1
    assert chat_harness.gateway.calls[0]["tools"] is None
    resumed_system = chat_harness.gateway.calls[0]["messages"][0].content
    assert fixed_compaction.record.compaction_id in resumed_system
    assert newer_result.view.record.compaction_id not in resumed_system
    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert [item.role for item in messages[-3:]] == ["user", "assistant", "assistant"]
    assert messages[-3].id == user_message.id


def test_resume_stream_restores_fixed_memory_reference_binding(
    chat_harness: ChatHarness,
) -> None:
    dataset_ref = "1" * 32
    _register_dataset(
        chat_harness.store,
        chat_harness.conversation,
        dataset_ref,
    )
    memory_id = _remember_agent_reference(
        chat_harness.store,
        chat_harness.conversation,
        alias="固定数据",
        target_ref=dataset_ref,
        key="checkpoint",
    )
    _, user_message = chat_harness.store.start_user_turn(
        conversation_id=chat_harness.conversation.id,
        content="总结固定数据",
        suggested_title="总结固定数据",
    )
    run_id = "memory-reference-recovery-run"
    contract = build_minimal_contract(
        run_id=run_id,
        user_text="总结固定数据",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    tasks = TaskStore(chat_harness.store.db_path)
    run, _ = tasks.create_run(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    memories = MemoryStore(chat_harness.store)
    snapshot, _ = memories.create_snapshot(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        run_id=run.run_id,
        principal=Principal(user_id="local-user"),
    )
    resolution = MemoryReferenceResolver(
        chat_harness.store,
        memories,
        audit_recorder=lambda _event: None,
    ).resolve(
        "总结固定数据",
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        memory_snapshot_id=snapshot.memory_snapshot_id,
        principal=Principal(user_id="local-user"),
    )
    run, _, _, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan={
            "schema_version": 1,
            "summary": "使用固定映射完成答复",
            "steps": [],
            "assumptions": list(resolution.assumptions()),
            "clarifications": [],
        },
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    paused, _, _ = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="pause-memory-reference",
        command="pause",
        allowed_statuses={"running"},
        status="paused",
        event_type="run.paused",
        payload={"reason": "process_recovery"},
        require_idle=True,
        checkpoint_reason="process_recovery",
    )

    response = chat_harness.client.post(
        f"/agent/runs/{run.run_id}/resume/stream",
        headers={
            "Idempotency-Key": "resume-memory-reference",
            "If-Match": str(paused.state_version),
        },
    )

    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert events[1][0] == "meta"
    assert events[1][1]["memory_snapshot_id"] == snapshot.memory_snapshot_id
    assert events[1][1]["memory_reference_status"] == "resolved"
    assert events[-1][1]["run_status"] == "completed"
    system = chat_harness.gateway.calls[0]["messages"][0].content
    assert memory_id in system
    active_plan = tasks.get_active_plan(run.run_id)
    assert active_plan is not None
    assert (
        find_memory_reference_assumptions(active_plan.plan.get("assumptions", []))
        == resolution.assumptions()
    )


def test_clarification_answer_reconstructs_lost_host(
    chat_harness: ChatHarness,
) -> None:
    _, user_message = chat_harness.store.start_user_turn(
        conversation_id=chat_harness.conversation.id,
        content="请确认回复方式",
        suggested_title="请确认回复方式",
    )
    run_id = "clarification-recovery-run"
    contract = build_minimal_contract(
        run_id=run_id,
        user_text="请确认回复方式",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    tasks = TaskStore(chat_harness.store.db_path)
    run, _ = tasks.create_run(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    question_id = "q_style"
    resume_token = "resume-token-after-restart"
    run, plan, _, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan={
            "schema_version": 1,
            "summary": "等待回复方式",
            "steps": [],
            "assumptions": [],
            "clarifications": [
                {
                    "question_id": question_id,
                    "question": "请选择回复方式",
                    "about": "style",
                    "blocking": True,
                }
            ],
        },
        reason="initial:fast",
        planner={"route": "fast"},
    )
    waiting, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="waiting_user",
        event_type="waiting_user",
        payload={
            "question_id": question_id,
            "question": "请选择回复方式",
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "resume_token": resume_token,
        },
        checkpoint_reason="waiting_user",
    )

    response = chat_harness.client.post(
        (f"/agent/runs/{run.run_id}/clarifications/" f"{question_id}/answer/stream"),
        headers={
            "Idempotency-Key": "answer-after-restart",
            "If-Match": str(waiting.state_version),
        },
        json={"answer": "简洁", "resume_token": resume_token},
    )

    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert [name for name, _ in events] == [
        "clarification.answered",
        "meta",
        "plan.revised",
        "run.started",
        "verification.started",
        "verification",
        "text.delta",
        "text.delta",
        "done",
    ], events
    assert events[-1][1]["run_status"] == "completed"
    saved = tasks.get_run(run.run_id)
    assert saved is not None and saved.status == "completed"
    assert saved.plan_version == 2
    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert len([item for item in messages if item.role == "user"]) == 2
    assert "简洁" in messages[1].content


def test_memory_reference_clarification_selects_once_without_writing(
    chat_harness: ChatHarness,
) -> None:
    first_ref = "1" * 32
    second_ref = "2" * 32
    _register_dataset(
        chat_harness.store,
        chat_harness.conversation,
        first_ref,
    )
    _register_dataset(
        chat_harness.store,
        chat_harness.conversation,
        second_ref,
    )
    _remember_agent_reference(
        chat_harness.store,
        chat_harness.conversation,
        alias="当前批次",
        target_ref=first_ref,
        key="answer-project",
    )
    selected_memory_id = _remember_agent_reference(
        chat_harness.store,
        chat_harness.conversation,
        alias="当前批次",
        target_ref=second_ref,
        key="answer-conversation",
        scope="conversation",
    )
    _, user_message = chat_harness.store.start_user_turn(
        conversation_id=chat_harness.conversation.id,
        content="总结当前批次",
        suggested_title="总结当前批次",
    )
    run_id = "memory-reference-answer-run"
    tasks = TaskStore(chat_harness.store.db_path)
    contract = build_minimal_contract(
        run_id=run_id,
        user_text="总结当前批次",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    memories = MemoryStore(chat_harness.store)
    snapshot, snapshot_records = memories.create_snapshot(
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        run_id=run.run_id,
        principal=Principal(user_id="local-user"),
    )
    question_id = "memory_reference_target"
    resume_token = "memory-reference-answer-token"
    run, plan, _, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan={
            "schema_version": 1,
            "summary": "等待选择受治理映射",
            "steps": [],
            "assumptions": [],
            "clarifications": [
                {
                    "question_id": question_id,
                    "question": "请选择受治理映射",
                    "about": "memory_reference_target",
                    "blocking": True,
                }
            ],
        },
        reason="initial:fast",
        planner={"route": "fast"},
    )
    waiting, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="waiting_user",
        event_type="waiting_user",
        payload={
            "question_id": question_id,
            "question": "请选择受治理映射",
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "resume_token": resume_token,
        },
        checkpoint_reason="waiting_user",
    )

    response = chat_harness.client.post(
        f"/agent/runs/{run.run_id}/clarifications/{question_id}/answer/stream",
        headers={
            "Idempotency-Key": "answer-memory-reference",
            "If-Match": str(waiting.state_version),
        },
        json={
            "answer": f"memory_id={selected_memory_id}",
            "resume_token": resume_token,
        },
    )

    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert events[1][1]["memory_snapshot_id"] == snapshot.memory_snapshot_id
    assert events[1][1]["memory_reference_status"] == "resolved"
    assert events[-1][1]["run_status"] == "completed"
    active_plan = tasks.get_active_plan(run.run_id)
    assert active_plan is not None
    assumptions = find_memory_reference_assumptions(active_plan.plan.get("assumptions", []))
    assert len(assumptions) == 1
    assert selected_memory_id in assumptions[0]
    governed_after = memories.list_records_for_governance(
        project_id=chat_harness.conversation.project_id,
        principal=Principal(user_id="local-user"),
        conversation_id=chat_harness.conversation.id,
    )
    assert len(governed_after) == len(snapshot_records)


def test_stream_context_contains_latest_profile_and_limited_history(
    chat_harness: ChatHarness,
) -> None:
    profile = {
        "row_count": 24,
        "column_count": 2,
        "columns": [{"name": "订单数"}, {"name": "销售额"}],
    }
    chat_harness.store.record_profile_upload(
        ref="sales-profile",
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        filename="销售.xlsx",
        profile=profile,
        user_content="上传了文件：销售.xlsx",
        assistant_content="画像完成",
    )
    chat_harness.store.append_message(
        conversation_id=chat_harness.conversation.id,
        role="user",
        content="这条历史会被截掉",
    )
    chat_harness.store.append_message(
        conversation_id=chat_harness.conversation.id,
        role="assistant",
        content="旧回复",
    )

    response = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "当前问题"},
    )

    assert response.status_code == 200
    request = chat_harness.gateway.calls[0]["messages"]
    assert len(request) == 4  # system + 最近 3 条消息
    system = request[0].content
    assert "sales-profile" in system  # 数据集清单必须给出 dataset_ref 供模型调工具
    assert '"row_count":24' in system
    assert "订单数" in system
    assert "分析登记表" in system  # 上传画像工件已入登记表
    assert [message.content for message in request[1:]] == [
        "这条历史会被截掉",
        "旧回复",
        "当前问题",
    ]
    conversation = chat_harness.store.get_conversation(chat_harness.conversation.id)
    assert conversation is not None and conversation.title == "销售.xlsx"


def test_stream_model_failure_emits_error_and_does_not_persist_partial_assistant(
    chat_harness: ChatHarness,
) -> None:
    chat_harness.gateway.turns = [
        {
            "deltas": ["部分回复"],
            "error": RuntimeError("provider disconnected"),
            "fail_after_deltas": True,
        },
    ]

    response = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "测试失败"},
    )

    events = _sse_events(response.text)
    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "run.started",
        "run.failed",
        "error",
    ]
    assert events[-1][1] == {
        "code": "model_unavailable",
        "message": "模型暂时不可用，请稍后重试。",
        "retryable": True,
    }
    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert [(message.role, message.content) for message in messages] == [("user", "测试失败")]


def test_stream_empty_response_is_not_persisted(chat_harness: ChatHarness) -> None:
    chat_harness.gateway.turns = [{"deltas": []}]

    response = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "空响应"},
    )

    events = _sse_events(response.text)
    assert [name for name, _ in events] == [
        "meta",
        "goal",
        "plan.created",
        "run.started",
        "verification.started",
        "verification",
        "error",
    ]
    assert events[-1][1]["code"] == "empty_response"
    assert [
        message.role for message in chat_harness.store.list_messages(chat_harness.conversation.id)
    ] == ["user"]


def test_stream_validates_request_before_model_call(chat_harness: ChatHarness) -> None:
    missing = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": "missing", "message": "你好"},
    )
    blank = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "   "},
    )

    assert missing.status_code == 404
    assert missing.json()["detail"] == "对话不存在"
    assert blank.status_code == 422
    assert chat_harness.gateway.calls == []


@pytest.mark.asyncio
async def test_conversation_lock_pool_serializes_same_conversation() -> None:
    pool = ConversationLockPool()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with pool.hold("same"):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-leave")

    async def second() -> None:
        await first_entered.wait()
        async with pool.hold("same"):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-leave", "second-enter"]
