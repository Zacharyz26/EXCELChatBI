"""Collect a repeatable observable-behavior baseline for the reactive Agent loop.

The harness uses real isolated model candidates with deterministic fixture tools.
It stores hashes and metrics, never raw dataset rows, secrets, full prompts or
full model responses.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.agent_loop import (  # noqa: E402
    AgentLoopConfig,
    ConversationLockPool,
    stream_agent_chat,
)
from packages.models.gateway import ModelGateway  # noqa: E402
from packages.models.registry import ModelRegistry  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402
from packages.session.models import Conversation, JsonObject  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402
from packages.session.task_store import TaskStore  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "agent_eval_set.jsonl"
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?%?")
_QUESTION_PATTERN = re.compile(r"(?:请确认|请选择|需要您|哪一|哪个|是否).*[？?]?")
_CAUSAL_PATTERN = re.compile(r"(?:导致|驱动|造成|因为.+所以)")
_CAPABILITY_BY_TOOL = {
    "get_data_profile": "data.profile",
    "kb_search": "knowledge.search",
    "aggregate_preview": "data.aggregate",
    "trend_analysis": "stats.trend",
    "anomaly_detect": "stats.anomaly",
    "transform_dataset": "dataset.transform",
    "correlation": "stats.correlation",
    "gen_chart": "visualization.chart",
    "generate_report": "report.generate",
}
_CAPABILITIES_BY_TOOL = {
    "get_data_profile": ("data.profile", "data.quality"),
    "kb_search": ("knowledge.search",),
    "aggregate_preview": ("data.aggregate",),
    "trend_analysis": ("stats.trend",),
    "anomaly_detect": ("stats.anomaly",),
    "transform_dataset": ("dataset.transform",),
    "regression": ("stats.regression",),
    "correlation": ("stats.correlation",),
    "gen_chart": ("visualization.chart",),
    "chart_screenshot": ("visualization.screenshot",),
    "generate_report": ("report.generate",),
}
_ARTIFACT_TYPES_BY_CAPABILITY = {
    "data.profile": ["profile"],
    "data.quality": ["profile"],
    "data.aggregate": ["table"],
    "stats.trend": ["stats"],
    "stats.anomaly": ["stats"],
    "stats.regression": ["stats"],
    "stats.correlation": ["stats"],
    "visualization.chart": ["chart"],
    "knowledge.search": ["citations"],
    "report.generate": ["report"],
}
_MEDIUM_RISK_CAPABILITIES = {
    "dataset.transform",
    "visualization.screenshot",
    "report.generate",
}
_ARTIFACT_EXPECTED_TYPE = {
    "profile": "profile",
    "citations": "citations",
    "table": "table",
    "chart": "chart",
    "report:pdf": "report",
}


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"基线场景第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"基线场景第 {line_number} 行顶层必须是对象")
        case_id = _required_text(raw.get("id"), f"第 {line_number} 行 id")
        if case_id in seen:
            raise ValueError(f"基线 case id 重复: {case_id}")
        seen.add(case_id)
        expected = _object(raw.get("expected"), f"{case_id}.expected")
        cases.append(
            {
                "id": case_id,
                "split": _required_text(raw.get("split"), f"{case_id}.split"),
                "category": _required_text(raw.get("category"), f"{case_id}.category"),
                "request": _required_text(raw.get("request"), f"{case_id}.request"),
                "context": _object(raw.get("context"), f"{case_id}.context"),
                "planner_expected": _object(
                    expected.get("planner"), f"{case_id}.expected.planner"
                ),
                "verifier_expected": _object(
                    expected.get("verifier"), f"{case_id}.expected.verifier"
                ),
                "forbidden": _string_list(
                    raw.get("forbidden", []), f"{case_id}.forbidden"
                ),
            }
        )
    if not cases:
        raise ValueError("基线场景不能为空")
    return cases


class _ObservingGateway:
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway
        self.responses: list[ModelResponse] = []

    async def stream_turn(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str | ModelResponse]:
        try:
            async with asyncio.timeout(45):
                async for item in self._gateway.stream_turn(
                    scenario, messages, tools=tools, params=params
                ):
                    if isinstance(item, ModelResponse):
                        self.responses.append(item)
                    yield item
        except TimeoutError as exc:
            raise RuntimeError("Agent 模型轮次超过 45 秒总时限") from exc

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        """Observe Planner calls without retaining prompts or response content."""
        try:
            async with asyncio.timeout(45):
                response = await self._gateway.complete(
                    scenario,
                    messages,
                    params=params,
                )
        except TimeoutError as exc:
            raise RuntimeError("Planner 模型轮次超过 45 秒总时限") from exc
        self.responses.append(response)
        return response


class _FixtureRegistry:
    """Schema-guided deterministic tools used only by the behavior baseline."""

    def __init__(
        self,
        case_id: str,
        workspace: Path,
        *,
        store: SessionStore | None = None,
        project_id: str | None = None,
    ) -> None:
        self.case_id = case_id
        self.workspace = workspace
        self.store = store
        self.project_id = project_id
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def openai_tools(
        self,
        *,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        tools = [
            _tool("get_data_profile", "获取数据画像与质量概况。", ["dataset_ref"]),
            _tool(
                "trend_analysis",
                "分析时间趋势；需要 dataset_ref、time_col、value_col。",
                ["dataset_ref", "time_col", "value_col"],
            ),
            _tool(
                "anomaly_detect",
                "检测异常点并返回行号；需要 dataset_ref、value_col。",
                ["dataset_ref", "value_col"],
            ),
            _tool(
                "regression",
                "回归分析；需要 dataset_ref、target、features。",
                ["dataset_ref", "target", "features"],
            ),
            _tool(
                "correlation",
                "相关性分析；需要 dataset_ref、columns，不能证明因果。",
                ["dataset_ref", "columns"],
            ),
            _tool(
                "gen_chart",
                "生成真实 ECharts 图表工件；需要 dataset_ref、chart_type、encoding。",
                ["dataset_ref", "chart_type", "encoding"],
            ),
            _tool("chart_screenshot", "把图表渲染为 PNG。", ["option"]),
            _tool(
                "transform_dataset",
                "按筛选或异常行号创建衍生数据集，返回新 dataset_ref。",
                ["dataset_ref"],
            ),
            _tool(
                "aggregate_preview",
                "按维度聚合指标；需要 dataset_ref、group_by、value_col、agg。",
                ["dataset_ref", "group_by", "value_col", "agg"],
            ),
            _tool("kb_search", "检索业务定义并返回真实来源。", ["query"]),
            _tool(
                "generate_report",
                "基于已有 analysis_ids 生成报告；用户要求 PDF 时 include_pdf=true。",
                ["analysis_ids", "title", "include_pdf"],
            ),
        ]
        return [
            item
            for item in tools
            if allowed_tool_names is None
            or str(item["function"]["name"]) in allowed_tool_names
        ]

    def capability_catalog(self) -> list[JsonObject]:
        """Expose the production Planner catalog shape for Stage 2 evaluation."""
        descriptions = {
            str(item["function"]["name"]): str(item["function"]["description"])
            for item in self.openai_tools()
        }
        catalog: list[JsonObject] = []
        for capability in sorted(
            {
                capability
                for values in _CAPABILITIES_BY_TOOL.values()
                for capability in values
            }
        ):
            tool_name = next(
                name
                for name, values in _CAPABILITIES_BY_TOOL.items()
                if capability in values
            )
            catalog.append(
                {
                    "name": capability,
                    "description": descriptions[tool_name],
                    "allowed": True,
                    "risk": (
                        "medium"
                        if capability in _MEDIUM_RISK_CAPABILITIES
                        else "low"
                    ),
                    "read_only": capability not in _MEDIUM_RISK_CAPABILITIES,
                    "artifact_types": _ARTIFACT_TYPES_BY_CAPABILITY.get(
                        capability,
                        [],
                    ),
                }
            )
        return catalog

    def openai_tools_for_capabilities(
        self,
        capabilities: set[str],
        *,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in self.openai_tools(allowed_tool_names=allowed_tool_names)
            if capabilities.intersection(
                self.capabilities_for_tool(str(item["function"]["name"]))
            )
        ]

    async def validate_remote_catalog(self) -> dict[str, str]:
        """Publish the deterministic in-memory fixture catalog for this run."""
        return {"fixture-tools": self._remote_catalog_hash()}

    def start_catalog_watch(self) -> None:
        """The immutable evaluation fixture has no runtime catalog changes."""

    def capability_catalog_snapshot(self) -> JsonObject:
        capabilities = self.capability_catalog()
        for item in capabilities:
            capability = str(item["name"])
            item["tool_names"] = [
                name
                for name, values in _CAPABILITIES_BY_TOOL.items()
                if capability in values
            ]
        return {
            "schema": "chatbi-capability-catalog-v1",
            "capabilities": capabilities,
            "tools": self._snapshot_tools(),
            "profiles": [],
            "remote_catalogs": [
                {
                    "service_name": "fixture-tools",
                    "content_hash": self._remote_catalog_hash(),
                }
            ],
        }

    def validate_capability_catalog_snapshot(
        self,
        snapshot: JsonObject,
    ) -> tuple[str, ...]:
        if snapshot.get("schema") != "chatbi-capability-catalog-v1":
            return ("unsupported_catalog_schema",)
        raw_tools = snapshot.get("tools")
        if not isinstance(raw_tools, list):
            return ("invalid_catalog_tools",)
        current = {
            str(item["tool_name"]): item for item in self._snapshot_tools()
        }
        issues: list[str] = []
        seen: set[str] = set()
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                issues.append("invalid_catalog_tool")
                continue
            tool_name = raw_tool.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name or tool_name in seen:
                issues.append("invalid_or_duplicate_tool_name")
                continue
            seen.add(tool_name)
            current_tool = current.get(tool_name)
            if current_tool is None:
                issues.append(f"tool_unavailable:{tool_name}")
                continue
            for key in (
                "service_name",
                "tool_version",
                "contract_hash",
                "capabilities",
            ):
                if raw_tool.get(key) != current_tool.get(key):
                    issues.append(f"tool_contract_drift:{tool_name}:{key}")
        return tuple(issues)

    @staticmethod
    def capability_catalog_from_snapshot(snapshot: JsonObject) -> list[JsonObject]:
        raw = snapshot.get("capabilities")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("TaskRun capability 目录快照格式非法")
        return [cast(JsonObject, item) for item in raw]

    @staticmethod
    def tool_names_from_snapshot(snapshot: JsonObject) -> frozenset[str]:
        raw = snapshot.get("tools")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("TaskRun tool 目录快照格式非法")
        names = [
            item.get("tool_name")
            for item in raw
            if item.get("allowed") is not False
        ]
        if not all(isinstance(item, str) and item for item in names):
            raise ValueError("TaskRun tool 目录快照包含非法工具名")
        return frozenset(cast(list[str], names))

    def _snapshot_tools(self) -> list[JsonObject]:
        return [
            {
                "service_name": "fixture-tools",
                "tool_name": str(item["function"]["name"]),
                "tool_version": "fixture-v1",
                "contract_hash": self._tool_contract_hash(item),
                "capabilities": list(
                    self.capabilities_for_tool(str(item["function"]["name"]))
                ),
                "allowed": True,
            }
            for item in self.openai_tools()
        ]

    def _remote_catalog_hash(self) -> str:
        encoded = json.dumps(
            self._snapshot_tools(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _tool_contract_hash(self, tool: dict[str, Any]) -> str:
        tool_name = str(tool["function"]["name"])
        encoded = json.dumps(
            {
                "tool": tool,
                "capabilities": list(self.capabilities_for_tool(tool_name)),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def capabilities_for_tool(self, tool_name: str) -> tuple[str, ...]:
        return _CAPABILITIES_BY_TOOL.get(tool_name, ())

    def execute(self, name: str, arguments_json: str) -> JsonObject:
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("工具入参不是合法 JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("工具入参必须是对象")
        self.executed.append((name, cast(dict[str, Any], arguments)))
        if self.case_id == "B17" and name == "trend_analysis":
            raise ValueError("样本不足，无法形成可靠预测")
        if self.case_id == "B18" and name == "gen_chart":
            raise ValueError("renderer_unavailable")
        if (
            self.case_id == "B16"
            and name == "trend_analysis"
            and "区域" in json.dumps(arguments, ensure_ascii=False)
        ):
            raise ValueError("区域不存在；唯一候选字段：地区")
        return self._result(name, cast(JsonObject, arguments))

    def _result(self, name: str, arguments: JsonObject) -> JsonObject:
        if name == "get_data_profile":
            return {
                "profile": {"row_count": 120, "column_count": 5},
                "quality": {"duplicate_rows": 2, "high_null_columns": []},
            }
        if name == "trend_analysis":
            return {
                "direction": "up",
                "n": 12,
                "time_scope": "2025-01/2025-12",
                "groups": ["华东", "华南"],
            }
        if name == "anomaly_detect":
            return {
                "n_total": 120,
                "n_anomalies": 4,
                "method": "IQR",
                "threshold": 1.5,
                "indices": [3, 17, 44, 88],
            }
        if name == "regression":
            return {"r_squared": 0.42, "n_obs": 120, "coefficients": {"折扣": -0.31}}
        if name == "correlation":
            return {
                "columns": ["折扣", "利润"],
                "n_obs": 120,
                "pairs": [{"left": "折扣", "right": "利润", "r": -0.46}],
            }
        if name == "gen_chart":
            return {
                "chart_type": str(arguments.get("chart_type", "bar")),
                "option": {
                    "xAxis": {"data": ["华东", "华南"]},
                    "series": [{"data": [120, 95]}],
                },
            }
        if name == "chart_screenshot":
            path = self.workspace / "chart.png"
            path.write_bytes(b"fixture-png")
            return {"png_path": str(path)}
        if name == "transform_dataset":
            parent_ref = str(arguments.get("dataset_ref", ""))
            derived_ref = _fixture_dataset_ref(
                self.case_id,
                f"derived:{len(self.executed)}:{parent_ref}",
            )
            if self.store is not None and self.project_id is not None:
                parent = self.store.get_dataset(parent_ref)
                self.store.register_dataset(
                    ref=derived_ref,
                    project_id=self.project_id,
                    filename="derived-fixture.xlsx",
                    profile=parent.profile if parent is not None else {},
                    parent_ref=parent_ref or None,
                    transform={"fixture": True},
                )
            return {
                "dataset_ref": derived_ref,
                "rows_before": 120,
                "rows_after": 116,
                "parent_ref": parent_ref,
            }
        if name == "aggregate_preview":
            return {
                "group_total": 2,
                "rows": [
                    {"地区": "华东", "销售额": 120},
                    {"地区": "华南", "销售额": 95},
                ],
            }
        if name == "kb_search":
            return {
                "hits": [
                    {
                        "source": "metrics/active-user.md",
                        "title": "活跃用户口径",
                        "snippet": "统计周期内至少完成一次有效访问的去重用户。",
                    }
                ]
            }
        if name == "generate_report":
            report_id = uuid.uuid4().hex
            md_path = self.workspace / f"{report_id}.md"
            md_path.write_text("# Fixture report\n", encoding="utf-8")
            result: JsonObject = {
                "report_id": report_id,
                "md_path": str(md_path),
                "skipped_charts": 0,
            }
            if arguments.get("include_pdf") is True:
                pdf_path = self.workspace / f"{report_id}.pdf"
                pdf_path.write_bytes(b"%PDF-1.4 fixture")
                result["pdf_path"] = str(pdf_path)
            return result
        raise ValueError(f"工具不存在: {name}")


async def run_evaluation(
    *,
    cases: list[dict[str, Any]],
    registry: ModelRegistry,
    model_names: list[str],
    repetitions: int,
    enforce_plan: bool = False,
    planner_model_name: str | None = None,
    evaluation_name: str = "reactive_agent_observable_baseline",
    evaluation_label: str = (
        "v2.3-compatible loop with stage-1 deterministic verifier"
    ),
    scenario_set_hash: str | None = None,
    existing_rows: list[dict[str, Any]] | None = None,
    on_row_completed: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    if enforce_plan and planner_model_name is None:
        raise ValueError("阶段 2 评测必须显式指定隔离 Planner 模型")
    concurrency = 4
    semaphore = asyncio.Semaphore(concurrency)
    pending: list[Any] = []
    rows = list(existing_rows or [])
    case_order = {str(case["id"]): index for index, case in enumerate(cases)}
    model_order = {model_name: index for index, model_name in enumerate(model_names)}
    expected_keys = {
        (model_name, repetition, str(case["id"]))
        for model_name in model_names
        for repetition in range(1, repetitions + 1)
        for case in cases
    }
    completed_keys: set[tuple[str, int, str]] = set()
    for row in rows:
        key = _evaluation_row_key(row)
        if key not in expected_keys:
            raise ValueError(f"恢复行不属于本次评测协议: {key}")
        if key in completed_keys:
            raise ValueError(f"恢复行重复: {key}")
        completed_keys.add(key)

    async def bounded(
        case: dict[str, Any],
        *,
        model_name: str,
        repetition: int,
        gateway: ModelGateway,
        planner_gateway: ModelGateway | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        async with semaphore:
            try:
                row = await _run_case(
                    case,
                    model_name=model_name,
                    repetition=repetition,
                    gateway=gateway,
                    planner_gateway=planner_gateway,
                    enforce_plan=enforce_plan,
                )
            except Exception as exc:
                return None, {
                    "case_id": str(case["id"]),
                    "configured_model": model_name,
                    "repetition": repetition,
                    "error_type": type(exc).__name__,
                }
            return row, None

    for model_name in model_names:
        isolated = registry.isolated_route(
            Scenario.AGENT,
            model_name,
            temperature=0.3,
            timeout_seconds=30,
            max_retries=0,
        )
        model_gateway = ModelGateway(isolated)
        planner_gateway = (
            ModelGateway(
                registry.isolated_route(
                    Scenario.COMPLEX_REASONING,
                    planner_model_name,
                    temperature=0.0,
                    timeout_seconds=30,
                    max_retries=0,
                )
            )
            if enforce_plan and planner_model_name is not None
            else None
        )
        for repetition in range(1, repetitions + 1):
            for case in cases:
                key = (model_name, repetition, str(case["id"]))
                if key in completed_keys:
                    continue
                pending.append(
                    bounded(
                        case,
                        model_name=model_name,
                        repetition=repetition,
                        gateway=model_gateway,
                        planner_gateway=planner_gateway,
                    )
                )
    failures: list[dict[str, Any]] = []
    for completed in asyncio.as_completed(pending):
        row, failure = await completed
        if failure is not None:
            failures.append(failure)
            continue
        if row is None:
            raise RuntimeError("评测任务既没有结果也没有失败记录")
        rows.append(row)
        if on_row_completed is not None:
            on_row_completed(list(rows))
    rows.sort(
        key=lambda row: (
            model_order[str(row["configured_model"])],
            int(row["repetition"]),
            case_order[str(row["case_id"])],
        )
    )
    failures.sort(
        key=lambda failure: (
            model_order[str(failure["configured_model"])],
            int(failure["repetition"]),
            case_order[str(failure["case_id"])],
        )
    )
    metrics = {
        model_name: _score_rows(
            [row for row in rows if row["configured_model"] == model_name]
        )
        for model_name in model_names
    }
    return {
        "schema_version": 1,
        "evaluation": evaluation_name,
        "baseline_label": evaluation_label,
        "execution_mode": "stage2_structured_plan" if enforce_plan else "v23_baseline",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "scenario_set_hash": (
            scenario_set_hash
            if scenario_set_hash is not None
            else hashlib.sha256(DEFAULT_CASES.read_bytes()).hexdigest()
        ),
        "repetitions": repetitions,
        "concurrency": concurrency,
        "models": model_names,
        "planner_model": planner_model_name,
        "case_count": len(cases),
        "expected_runs": len(expected_keys),
        "completed_runs": len(rows),
        "execution_failures": failures,
        "metrics": metrics,
        "rows": rows,
        "privacy": {
            "raw_dataset_rows_stored": False,
            "full_prompts_stored": False,
            "full_model_responses_stored": False,
            "secrets_stored": False,
        },
    }


def _evaluation_row_key(row: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("configured_model", "")),
        int(row.get("repetition", 0)),
        str(row.get("case_id", "")),
    )


async def _run_case(
    case: dict[str, Any],
    *,
    model_name: str,
    repetition: int,
    gateway: ModelGateway,
    planner_gateway: ModelGateway | None = None,
    enforce_plan: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chatbi-baseline-") as raw_workspace:
        workspace = Path(raw_workspace)
        store = SessionStore(str(workspace / "chatbi.db"))
        project = store.create_project(f"baseline-{case['id']}")
        conversation = store.create_conversation(project.id)
        _seed_context(store, conversation, case, workspace)
        observing = _ObservingGateway(gateway)
        observing_planner = (
            _ObservingGateway(planner_gateway)
            if planner_gateway is not None
            else None
        )
        fixture_registry = _FixtureRegistry(
            str(case["id"]),
            workspace,
            store=store,
            project_id=project.id,
        )
        raw_events = [
            item
            async for item in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                user_text=str(case["request"]),
                store=store,
                gateway=observing,
                registry=cast(Any, fixture_registry),
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=12, tool_result_max_chars=4_000),
                planner_gateway=observing_planner,
                enforce_plan=enforce_plan,
            )
        ]
        events = [
            (item["event"], cast(dict[str, Any], json.loads(item["data"])))
            for item in raw_events
        ]
        return _observable_row(
            case,
            model_name=model_name,
            repetition=repetition,
            events=events,
            responses=[
                *(observing_planner.responses if observing_planner is not None else []),
                *observing.responses,
            ],
            planner_responses=(
                observing_planner.responses if observing_planner is not None else []
            ),
            store=store,
            conversation=conversation,
            executed=fixture_registry.executed,
            execution_mode=(
                "stage2_structured_plan" if enforce_plan else "v23_baseline"
            ),
        )


def _observable_row(
    case: dict[str, Any],
    *,
    model_name: str,
    repetition: int,
    events: list[tuple[str, dict[str, Any]]],
    responses: list[ModelResponse],
    planner_responses: list[ModelResponse],
    store: SessionStore,
    conversation: Conversation,
    executed: list[tuple[str, dict[str, Any]]],
    execution_mode: str,
) -> dict[str, Any]:
    done = next((payload for name, payload in reversed(events) if name == "done"), None)
    error = next((payload for name, payload in reversed(events) if name == "error"), None)
    meta = next((payload for name, payload in events if name == "meta"), {})
    run_id = str(meta.get("run_id", ""))
    task_store = TaskStore(store.db_path)
    run = task_store.get_run(run_id) if run_id else None
    plan = task_store.get_active_plan(run_id) if run_id else None
    plan_steps = task_store.list_plan_steps(run_id) if run_id else []
    task_events = task_store.list_events(run_id, limit=1_000) if run_id else []
    claims = task_store.list_claims(run_id) if run_id else []
    evidence = task_store.list_evidence(run_id) if run_id else []
    artifacts = store.list_artifacts(conversation.id)
    event_artifact_ids = {
        str(payload.get("id"))
        for name, payload in events
        if name == "artifact" and payload.get("id")
    }
    tool_end = [payload for name, payload in events if name == "tool_end"]
    successful_tools = [
        str(payload.get("tool"))
        for payload in tool_end
        if payload.get("status") == "ok"
    ]
    capabilities = (
        {
            capability
            for name in successful_tools
            for capability in _CAPABILITIES_BY_TOOL.get(name, ())
        }
        if execution_mode == "stage2_structured_plan"
        else {
            _CAPABILITY_BY_TOOL[name]
            for name in successful_tools
            if name in _CAPABILITY_BY_TOOL
        }
    )
    final_text = "".join(
        str(payload.get("delta", ""))
        for name, payload in events
        if name == "text.delta"
    )
    required_capabilities = set(
        _string_list(
            cast(dict[str, Any], case["planner_expected"]).get(
                "required_capabilities", []
            ),
            "required_capabilities",
        )
    )
    conditional_capabilities = set(
        _string_list(
            cast(dict[str, Any], case["planner_expected"]).get(
                "conditional_capabilities", []
            ),
            "conditional_capabilities",
        )
    )
    blocking_expected = (
        cast(dict[str, Any], case["planner_expected"]).get("clarification")
        == "blocking"
    )
    clarification_detected = bool(_QUESTION_PATTERN.search(final_text))
    required_artifacts = _string_list(
        cast(dict[str, Any], case["planner_expected"]).get("required_artifacts", []),
        "required_artifacts",
    )
    artifact_checks = {
        item: _artifact_satisfied(item, artifacts, event_artifact_ids)
        for item in required_artifacts
    }
    numerical_claims = [
        claim for claim in claims if _NUMBER_PATTERN.search(claim.statement)
    ]
    numeric_claims_supported = all(
        claim.evidence_ids and claim.value_refs for claim in numerical_claims
    )
    expected_terminal = str(
        cast(dict[str, Any], case["verifier_expected"]).get("verdict", "PASS")
    )
    actual_status = run.status if run is not None else "not_started"
    terminal_truthful = _terminal_matches(
        expected_terminal,
        actual_status,
        clarification_detected=clarification_detected,
    )
    required_effective = required_capabilities - (
        conditional_capabilities if not {"stats.anomaly"}.issubset(capabilities) else set()
    )
    capabilities_satisfied = (
        True
        if blocking_expected and clarification_detected
        else required_effective.issubset(capabilities)
    )
    forbidden_violations = _forbidden_violations(
        case,
        final_text=final_text,
        artifact_checks=artifact_checks,
        executed=executed,
        capabilities=capabilities,
    )
    task_satisfied = (
        capabilities_satisfied
        and all(artifact_checks.values())
        and numeric_claims_supported
        and terminal_truthful
        and not forbidden_violations
    )
    costs: list[float] = []
    for response in responses:
        if response.cost is not None:
            costs.append(float(response.cost))
    currencies = {
        response.cost_currency
        for response in responses
        if response.cost_currency is not None
    }
    return {
        "case_id": case["id"],
        "split": case["split"],
        "category": case["category"],
        "repetition": repetition,
        "configured_model": model_name,
        "actual_models": sorted({response.model for response in responses}),
        "request_hash": _json_hash(
            {"request": case["request"], "context": case["context"]}
        ),
        "final_response_hash": (
            hashlib.sha256(final_text.encode("utf-8")).hexdigest()
            if final_text
            else None
        ),
        "task_satisfied": task_satisfied,
        "required_capabilities_satisfied": capabilities_satisfied,
        "successful_capabilities": sorted(capabilities),
        "required_artifacts": artifact_checks,
        "artifacts_persisted": len(
            [artifact for artifact in artifacts if artifact.id in event_artifact_ids]
        ),
        "artifacts_sent_frontend": len(event_artifact_ids),
        "numeric_claims_supported": numeric_claims_supported,
        "numeric_claim_count": len(numerical_claims),
        "evidence_count": len(evidence),
        "tool_calls": len(executed),
        "invalid_tool_calls": sum(
            payload.get("status") == "error" for payload in tool_end
        ),
        "clarification": (
            "correct"
            if blocking_expected and clarification_detected
            else "missed"
            if blocking_expected
            else "excessive"
            if clarification_detected
            else "none"
        ),
        "expected_terminal": expected_terminal,
        "actual_terminal": actual_status,
        "terminal_truthful": terminal_truthful,
        "forbidden_violations": forbidden_violations,
        "model_calls": len(responses),
        "agent_model_calls": len(responses) - len(planner_responses),
        "planner_model_calls": len(planner_responses),
        "plan_version": run.plan_version if run is not None else 0,
        "planner_route": next(
            (
                str(event.payload.get("planner_route"))
                for event in task_events
                if event.event_type == "run.started"
                and event.payload.get("planner_route")
            ),
            None,
        ),
        "planned_capabilities": (
            sorted(
                str(step.get("capability"))
                for step in cast(list[JsonObject], plan.plan.get("steps", []))
            )
            if plan is not None
            else []
        ),
        "plan_steps_total": len(plan_steps),
        "plan_steps_completed": sum(
            step.status in {"completed", "skipped"} for step in plan_steps
        ),
        "plan_revisions": sum(
            event.event_type == "plan.revised" for event in task_events
        ),
        "prompt_tokens": sum(response.prompt_tokens for response in responses),
        "completion_tokens": sum(
            response.completion_tokens for response in responses
        ),
        "usage_available": all(response.usage_available for response in responses)
        if responses
        else False,
        "latency_ms": round(sum(response.latency_ms for response in responses), 3),
        "cost": round(sum(costs), 9) if costs else None,
        "cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "cost_availability": (
            "available"
            if responses and len(costs) == len(responses) and len(currencies) == 1
            else "unavailable"
        ),
        "error_code": error.get("code") if error else None,
        "done_emitted": done is not None,
    }


def _score_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    costs = [float(row["cost"]) for row in rows if row.get("cost") is not None]
    cost_complete = all(
        int(row.get("model_calls", 0)) == 0 or row.get("cost") is not None
        for row in rows
    )
    currencies = {
        str(row["cost_currency"])
        for row in rows
        if row.get("cost_currency") is not None
    }
    return {
        "runs": total,
        "task_success_rate": (
            sum(bool(row["task_satisfied"]) for row in rows) / total if total else 0.0
        ),
        "artifact_delivery_rate": _mean(
            [
                all(cast(dict[str, bool], row["required_artifacts"]).values())
                for row in rows
                if row["required_artifacts"]
            ]
        ),
        "numeric_claim_support_rate": _mean(
            [bool(row["numeric_claims_supported"]) for row in rows]
        ),
        "truthful_terminal_rate": _mean(
            [bool(row["terminal_truthful"]) for row in rows]
        ),
        "clarification_accuracy": _mean(
            [
                row["clarification"] in {"correct", "none"}
                for row in rows
            ]
        ),
        "tool_calls": sum(int(row["tool_calls"]) for row in rows),
        "invalid_tool_calls": sum(int(row["invalid_tool_calls"]) for row in rows),
        "forbidden_violations": sum(
            len(cast(list[str], row["forbidden_violations"])) for row in rows
        ),
        "model_calls": sum(int(row["model_calls"]) for row in rows),
        "agent_model_calls": sum(int(row.get("agent_model_calls", 0)) for row in rows),
        "planner_model_calls": sum(
            int(row.get("planner_model_calls", 0)) for row in rows
        ),
        "plan_completion_rate": _mean(
            [
                int(row.get("plan_steps_completed", 0))
                == int(row.get("plan_steps_total", 0))
                for row in rows
                if int(row.get("plan_steps_total", 0)) > 0
            ]
        ),
        "runs_replanned": sum(
            int(row.get("plan_revisions", 0)) > 0 for row in rows
        ),
        "prompt_tokens": sum(int(row["prompt_tokens"]) for row in rows),
        "completion_tokens": sum(int(row["completion_tokens"]) for row in rows),
        "latency_ms": round(sum(float(row["latency_ms"]) for row in rows), 3),
        "cost": (
            round(sum(costs), 9)
            if cost_complete and (costs or rows)
            else None
        ),
        "cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "cost_availability": (
            "available" if rows and cost_complete else "unavailable"
        ),
    }


def _seed_context(
    store: SessionStore,
    conversation: Conversation,
    case: dict[str, Any],
    workspace: Path,
) -> None:
    context = cast(dict[str, Any], case["context"])
    for dataset in cast(list[dict[str, Any]], context.get("datasets") or []):
        columns = [str(item) for item in cast(list[object], dataset.get("columns") or [])]
        source_ref = str(dataset["ref"])
        store.register_dataset(
            ref=_fixture_dataset_ref(str(case["id"]), source_ref),
            project_id=conversation.project_id,
            filename=f"{source_ref}.xlsx",
            profile={
                "row_count": int(dataset.get("row_count", 120)),
                "column_count": len(columns),
                "columns": [{"name": column} for column in columns],
            },
        )

    artifacts = cast(list[dict[str, Any]], context.get("artifacts") or [])
    if artifacts:
        message = store.append_message(
            conversation_id=conversation.id,
            role="assistant",
            content="上一轮分析已产生以下工件。",
        )
        for item in artifacts:
            artifact_type = str(item.get("type", "stats"))
            payload: JsonObject = {"fixture_id": item.get("id")}
            file_ref: str | None = None
            if artifact_type == "chart":
                payload["option"] = {"series": [{"data": [1, 2]}]}
            if artifact_type == "report":
                file_ref = str(workspace / "missing.pdf")
                payload["pdf_url"] = "/reports/missing.pdf"
            store.create_artifact(
                conversation_id=conversation.id,
                message_id=message.id,
                type=artifact_type,
                payload=payload,
                file_ref=file_ref,
                source_tool="fixture",
                params=cast(JsonObject, item.get("params") or {}),
            )
    observations = cast(list[dict[str, Any]], context.get("observations") or [])
    conflicts = cast(list[dict[str, Any]], context.get("knowledge_conflicts") or [])
    notes: list[str] = []
    for item in observations:
        notes.append(
            f"上次 {item.get('tool', '工具')} 返回 {item.get('code', 'error')}："
            f"{item.get('detail', '')}"
        )
    for item in conflicts:
        definitions = "；".join(
            str(value) for value in cast(list[object], item.get("definitions") or [])
        )
        notes.append(f"{item.get('concept', '指标')}存在冲突口径：{definitions}")
    if notes:
        store.append_message(
            conversation_id=conversation.id,
            role="assistant",
            content="\n".join(notes),
        )


def _fixture_dataset_ref(case_id: str, alias: str) -> str:
    """Map readable fixture aliases to production-valid opaque references."""
    return hashlib.sha256(f"{case_id}:{alias}".encode()).hexdigest()[:32]


def _artifact_satisfied(
    requirement: str,
    artifacts: list[Any],
    event_artifact_ids: set[str],
) -> bool:
    artifact_type = _ARTIFACT_EXPECTED_TYPE.get(requirement)
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.type == artifact_type and artifact.id in event_artifact_ids
    ]
    if not candidates:
        return False
    if requirement == "report:pdf":
        return any(
            isinstance(artifact.payload, dict)
            and isinstance(artifact.payload.get("pdf_url"), str)
            and artifact.file_ref
            and Path(artifact.file_ref).is_file()
            and Path(artifact.file_ref).stat().st_size > 0
            for artifact in candidates
        )
    return True


def _terminal_matches(
    expected: str,
    actual_status: str,
    *,
    clarification_detected: bool,
) -> bool:
    if expected == "PASS":
        return actual_status == "completed"
    if expected == "WAITING_USER":
        return actual_status == "waiting_user" or (
            actual_status != "completed" and clarification_detected
        )
    if expected == "BLOCKED":
        return actual_status == "blocked"
    if expected == "FAILED":
        return actual_status == "failed"
    if expected == "NEEDS_ACTION":
        return actual_status in {"blocked", "failed"}
    return False


def _forbidden_violations(
    case: dict[str, Any],
    *,
    final_text: str,
    artifact_checks: dict[str, bool],
    executed: list[tuple[str, dict[str, Any]]],
    capabilities: set[str],
) -> list[str]:
    violations: list[str] = []
    forbidden = set(cast(list[str], case["forbidden"]))
    if "causal_claim" in forbidden or "causal_language" in forbidden:
        if _CAUSAL_PATTERN.search(final_text):
            violations.append("causal_language")
    if {"text_only_completion", "markdown_only_completion"} & forbidden:
        if artifact_checks and not all(artifact_checks.values()):
            violations.append("text_substituted_required_artifact")
    if "calculate_before_definition" in forbidden and any(
        name in {"aggregate_preview", "trend_analysis"} for name, _ in executed
    ):
        violations.append("calculated_before_definition")
    if "raw_table_to_model" in forbidden:
        # Fixture context contains no rows; any such leak is structurally impossible.
        pass
    if "model_arithmetic" in forbidden and "data.aggregate" not in capabilities:
        if _NUMBER_PATTERN.search(final_text):
            violations.append("numeric_answer_without_aggregate")
    return violations


def _tool(name: str, description: str, required: list[str]) -> dict[str, Any]:
    properties = {key: _parameter_schema(key) for key in required}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": True,
            },
        },
    }


def _parameter_schema(name: str) -> JsonObject:
    if name in {"features", "columns", "analysis_ids"}:
        return {"type": "array", "items": {"type": "string"}}
    if name == "include_pdf":
        return {"type": "boolean"}
    if name in {"encoding", "option"}:
        return {"type": "object"}
    return {"type": "string"}


def _mean(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return cast(dict[str, Any], value)


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} 必须是字符串数组")
    return [str(item).strip() for item in value]


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _print_report(report: dict[str, Any]) -> None:
    print(f"Baseline: {report['baseline_label']}")
    for model, metrics in cast(dict[str, dict[str, Any]], report["metrics"]).items():
        print(
            f"{model}: success={metrics['task_success_rate']:.1%} "
            f"invalid_calls={metrics['invalid_tool_calls']} "
            f"terminal={metrics['truthful_terminal_rate']:.1%} "
            f"cost={metrics['cost'] if metrics['cost'] is not None else 'unavailable'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.4 阶段0 v2.3 行为基线")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--registry", default="config/models.yaml")
    parser.add_argument("--models", help="逗号分隔的 registry model name")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--split", choices=("all", "public", "heldout"), default="all")
    parser.add_argument("--case-ids", help="逗号分隔 case id")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--json-output", help="报告路径；'-' 表示 stdout")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions 必须大于 0")

    cases = load_cases(args.cases)
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    if args.case_ids:
        requested = {item.strip() for item in args.case_ids.split(",") if item.strip()}
        available = {str(case["id"]) for case in cases}
        missing = requested - available
        if missing:
            parser.error(f"case id 不存在或不属于当前 split: {sorted(missing)}")
        cases = [case for case in cases if case["id"] in requested]
    if args.validate_only:
        print(f"Validated {len(cases)} baseline cases from {args.cases}")
        return 0

    registry = ModelRegistry(args.registry)
    registry.load()
    model_names = (
        [name.strip() for name in args.models.split(",") if name.strip()]
        if args.models
        else list(registry.route_candidates(Scenario.AGENT))
    )
    if not model_names:
        parser.error("没有可评测模型")
    for model_name in model_names:
        if not registry.get_model(model_name).supports_tools:
            parser.error(f"模型 {model_name} 不支持 tools，不能运行 Agent 基线")

    report = asyncio.run(
        run_evaluation(
            cases=cases,
            registry=registry,
            model_names=model_names,
            repetitions=args.repetitions,
        )
    )
    _print_report(report)
    if args.json_output == "-":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        output = (
            Path(args.json_output)
            if args.json_output
            else Path(".data/evaluations/v2.4")
            / f"baseline-{uuid.uuid4().hex[:12]}"
            / "report.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
