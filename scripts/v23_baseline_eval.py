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
from collections.abc import AsyncIterator
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


class _FixtureRegistry:
    """Schema-guided deterministic tools used only by the behavior baseline."""

    def __init__(self, case_id: str, workspace: Path) -> None:
        self.case_id = case_id
        self.workspace = workspace
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
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
            return {
                "dataset_ref": "derived-fixture",
                "rows_before": 120,
                "rows_after": 116,
                "parent_ref": arguments.get("dataset_ref"),
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
            report_id = f"eval-{uuid.uuid4().hex[:8]}"
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
) -> dict[str, Any]:
    concurrency = 4
    semaphore = asyncio.Semaphore(concurrency)
    pending: list[Any] = []

    async def bounded(
        case: dict[str, Any],
        *,
        model_name: str,
        repetition: int,
        gateway: ModelGateway,
    ) -> dict[str, Any]:
        async with semaphore:
            return await _run_case(
                case,
                model_name=model_name,
                repetition=repetition,
                gateway=gateway,
            )

    for model_name in model_names:
        isolated = registry.isolated_route(
            Scenario.AGENT,
            model_name,
            temperature=0.3,
            timeout_seconds=30,
            max_retries=0,
        )
        model_gateway = ModelGateway(isolated)
        for repetition in range(1, repetitions + 1):
            for case in cases:
                pending.append(
                    bounded(
                        case,
                        model_name=model_name,
                        repetition=repetition,
                        gateway=model_gateway,
                    )
                )
    rows = list(await asyncio.gather(*pending))
    metrics = {
        model_name: _score_rows(
            [row for row in rows if row["configured_model"] == model_name]
        )
        for model_name in model_names
    }
    return {
        "schema_version": 1,
        "evaluation": "reactive_agent_observable_baseline",
        "baseline_label": "v2.3-compatible loop with stage-1 deterministic verifier",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "scenario_set_hash": hashlib.sha256(DEFAULT_CASES.read_bytes()).hexdigest(),
        "repetitions": repetitions,
        "concurrency": concurrency,
        "models": model_names,
        "case_count": len(cases),
        "metrics": metrics,
        "rows": rows,
        "privacy": {
            "raw_dataset_rows_stored": False,
            "full_prompts_stored": False,
            "full_model_responses_stored": False,
            "secrets_stored": False,
        },
    }


async def _run_case(
    case: dict[str, Any],
    *,
    model_name: str,
    repetition: int,
    gateway: ModelGateway,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chatbi-baseline-") as raw_workspace:
        workspace = Path(raw_workspace)
        store = SessionStore(str(workspace / "chatbi.db"))
        project = store.create_project(f"baseline-{case['id']}")
        conversation = store.create_conversation(project.id)
        _seed_context(store, conversation, case, workspace)
        observing = _ObservingGateway(gateway)
        fixture_registry = _FixtureRegistry(str(case["id"]), workspace)
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
            responses=observing.responses,
            store=store,
            conversation=conversation,
            executed=fixture_registry.executed,
        )


def _observable_row(
    case: dict[str, Any],
    *,
    model_name: str,
    repetition: int,
    events: list[tuple[str, dict[str, Any]]],
    responses: list[ModelResponse],
    store: SessionStore,
    conversation: Conversation,
    executed: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    done = next((payload for name, payload in reversed(events) if name == "done"), None)
    error = next((payload for name, payload in reversed(events) if name == "error"), None)
    meta = next((payload for name, payload in events if name == "meta"), {})
    run_id = str(meta.get("run_id", ""))
    task_store = TaskStore(store.db_path)
    run = task_store.get_run(run_id) if run_id else None
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
    capabilities = {
        _CAPABILITY_BY_TOOL[name]
        for name in successful_tools
        if name in _CAPABILITY_BY_TOOL
    }
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
    costs = [response.cost for response in responses if response.cost is not None]
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
        "prompt_tokens": sum(response.prompt_tokens for response in responses),
        "completion_tokens": sum(
            response.completion_tokens for response in responses
        ),
        "usage_available": all(response.usage_available for response in responses)
        if responses
        else False,
        "latency_ms": round(sum(response.latency_ms for response in responses), 3),
        "cost": round(sum(cast(list[float], costs)), 9) if costs else None,
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
        "prompt_tokens": sum(int(row["prompt_tokens"]) for row in rows),
        "completion_tokens": sum(int(row["completion_tokens"]) for row in rows),
        "latency_ms": round(sum(float(row["latency_ms"]) for row in rows), 3),
        "cost": round(sum(costs), 9) if costs else None,
        "cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "cost_availability": (
            "available" if costs and len(costs) == total else "unavailable"
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
        store.register_dataset(
            ref=str(dataset["ref"]),
            project_id=conversation.project_id,
            filename=f"{dataset['ref']}.xlsx",
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
