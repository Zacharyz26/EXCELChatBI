"""Small deterministic model boundary for reproducible Compose browser E2E.

The API, Agent state machine, MCP transports, tools, SQLite, files, Web proxy and
browser are real. Only the non-deterministic external model provider is replaced.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="ChatBI E2E model fixture")

_BRANCH_MARKER = "COMPOSE_4D_BRANCH"
_FEEDBACK_MARKER = "COMPOSE_4D_FEEDBACK"
_READ_ONLY_MARKER = "COMPOSE_4D_READ_ONLY"
_PARALLEL_MARKER = "COMPOSE_6A_PARALLEL"
_HYPOTHESIS_MARKER = "请深入分析这份数据"
_REPORT_MARKER = "COMPOSE_REPORT"
_audit: dict[str, int | bool] = {
    "planner_calls": 0,
    "feedback_marker_seen_in_planner": False,
    "branch_profile_tool_calls": 0,
    "read_only_report_attempts": 0,
    "parallel_tool_batches": 0,
    "hypothesis_anomaly_tool_calls": 0,
}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/audit")
async def audit() -> dict[str, int | bool]:
    """Return bounded fixture facts; never expose model messages or prompt text."""
    return dict(_audit)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages required")
    model = str(payload.get("model") or "chatbi-e2e")
    joined = json.dumps(messages, ensure_ascii=False)
    if _is_planner_call(messages):
        _audit["planner_calls"] = int(_audit["planner_calls"]) + 1
        if _FEEDBACK_MARKER in joined:
            _audit["feedback_marker_seen_in_planner"] = True
    if payload.get("stream") is True:
        return StreamingResponse(
            _stream_turn(model, messages, payload.get("tools")),
            media_type="text/event-stream",
        )
    content = _complete_json(messages)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 32,
            "completion_tokens": 16,
            "total_tokens": 48,
        },
    }


async def _stream_turn(
    model: str,
    messages: list[Any],
    raw_tools: Any,
) -> AsyncIterator[str]:
    call_id = f"call-{uuid.uuid4().hex[:16]}"
    last_message = messages[-1] if messages else None
    has_current_tool_result = isinstance(last_message, dict) and last_message.get("role") == "tool"
    tools = raw_tools if isinstance(raw_tools, list) else []
    tool_names = [
        function.get("name")
        for item in tools
        if isinstance(item, dict) and isinstance((function := item.get("function")), dict)
    ]
    joined = json.dumps(messages, ensure_ascii=False)
    scenario_marker = _latest_scenario_marker(messages)
    if (
        not has_current_tool_result
        and scenario_marker == _PARALLEL_MARKER
        and {"get_data_profile", "trend_analysis"}.issubset(tool_names)
    ):
        dataset_refs = re.findall(r"最新数据集 ([0-9a-f]{32})", joined)
        if not dataset_refs:
            raise HTTPException(status_code=422, detail="dataset_ref missing")
        _audit["parallel_tool_batches"] = int(_audit["parallel_tool_batches"]) + 1
        dataset_ref = dataset_refs[-1]
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "我会在同一受控批次中并行检查数据画像和趋势。",
            },
        )
        yield _sse_chunk(
            model,
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"{call_id}-profile",
                        "type": "function",
                        "function": {
                            "name": "get_data_profile",
                            "arguments": json.dumps(
                                {"dataset_ref": dataset_ref},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    },
                    {
                        "index": 1,
                        "id": f"{call_id}-trend",
                        "type": "function",
                        "function": {
                            "name": "trend_analysis",
                            "arguments": json.dumps(
                                {
                                    "dataset_ref": dataset_ref,
                                    "value_col": "销售额",
                                    "time_col": "月份",
                                    "method": "ma",
                                    "forecast_horizon": 0,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    },
                ]
            },
        )
        yield _sse_chunk(model, {}, finish_reason="tool_calls")
    elif (
        not has_current_tool_result
        and scenario_marker == _HYPOTHESIS_MARKER
        and "anomaly_detect" in tool_names
    ):
        dataset_refs = re.findall(r"最新数据集 ([0-9a-f]{32})", joined)
        if not dataset_refs:
            raise HTTPException(status_code=422, detail="dataset_ref missing")
        _audit["hypothesis_anomaly_tool_calls"] = int(
            _audit["hypothesis_anomaly_tool_calls"]
        ) + 1
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "我会只验证用户选中的异常候选，并等待 Evidence。",
            },
        )
        yield _sse_chunk(
            model,
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "anomaly_detect",
                            "arguments": json.dumps(
                                {
                                    "dataset_ref": dataset_refs[-1],
                                    "value_col": "销售额",
                                    "method": "iqr",
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ]
            },
        )
        yield _sse_chunk(model, {}, finish_reason="tool_calls")
    elif (
        not has_current_tool_result
        and scenario_marker == _BRANCH_MARKER
        and "get_data_profile" in tool_names
    ):
        dataset_refs = re.findall(r"最新数据集 ([0-9a-f]{32})", joined)
        if not dataset_refs:
            raise HTTPException(status_code=422, detail="dataset_ref missing")
        _audit["branch_profile_tool_calls"] = int(_audit["branch_profile_tool_calls"]) + 1
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "我会按已确认计划重新核对数据规模与字段画像。",
            },
        )
        arguments = json.dumps(
            {"dataset_ref": dataset_refs[-1]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        yield _sse_chunk(
            model,
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "get_data_profile",
                            "arguments": arguments,
                        },
                    }
                ]
            },
        )
        yield _sse_chunk(model, {}, finish_reason="tool_calls")
    elif not has_current_tool_result and "generate_report" in tool_names:
        if scenario_marker == _READ_ONLY_MARKER:
            _audit["read_only_report_attempts"] = int(_audit["read_only_report_attempts"]) + 1
        analysis_ids = re.findall(r"analysis_id=([A-Za-z0-9_-]+)", joined)
        if not analysis_ids:
            raise HTTPException(status_code=422, detail="analysis_id missing")
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "我将把已有画像组装成报告并导出 PDF。",
            },
        )
        arguments = json.dumps(
            {
                "title": "销售数据分析报告",
                "analysis_ids": [analysis_ids[-1]],
                "insights": "本报告基于已验证的数据画像。",
                "include_pdf": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        yield _sse_chunk(
            model,
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "generate_report",
                            "arguments": arguments,
                        },
                    }
                ]
            },
        )
        yield _sse_chunk(model, {}, finish_reason="tool_calls")
    elif scenario_marker == _PARALLEL_MARKER:
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "Compose 6A 受控并行画像与趋势分析已完成。",
            },
        )
        yield _sse_chunk(model, {}, finish_reason="stop")
    elif scenario_marker == _HYPOTHESIS_MARKER:
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "Compose 6C 异常候选验证完成；Evidence 未支持该候选。",
            },
        )
        yield _sse_chunk(model, {}, finish_reason="stop")
    elif scenario_marker == _BRANCH_MARKER:
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "Compose 4D 分支画像已完成，并已按父分支反馈重新核对。",
            },
        )
        yield _sse_chunk(model, {}, finish_reason="stop")
    elif scenario_marker == _READ_ONLY_MARKER:
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "标准只读模式已阻止报告写入。",
            },
        )
        yield _sse_chunk(model, {}, finish_reason="stop")
    else:
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "报告和 PDF 已基于本对话的已验证数据画像生成。",
            },
        )
        yield _sse_chunk(model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _complete_json(messages: list[Any]) -> str:
    system = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "system"
    )
    raw_user = next(
        (
            str(message.get("content", ""))
            for message in reversed(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        "{}",
    )
    if "语义 Verifier" in system:
        payload = json.loads(raw_user)
        rules = payload["response_rules"]
        claim_id = rules["known_claim_ids"][0]
        evidence_id = rules["known_evidence_ids"][0]
        return json.dumps(
            {
                "schema_version": 1,
                "criteria": [
                    {
                        "criterion_id": criterion_id,
                        "status": "pass",
                        "reason": "最终答复与已提交 Evidence/Artifact 一致。",
                        "claim_ids": [claim_id],
                        "evidence_ids": [evidence_id],
                    }
                    for criterion_id in rules["criterion_ids"]
                ],
                "overclaims": [],
                "limitations_ok": True,
                "next_action": {
                    "kind": "accept",
                    "reason": "所有成功标准均有 Claim 与 Evidence 覆盖。",
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if "受约束任务 Planner" in system:
        payload = json.loads(raw_user)
        planning_request = str(payload.get("planning_request") or "")
        raw_catalog = payload.get("capability_catalog")
        catalog = raw_catalog if isinstance(raw_catalog, list) else []
        allowed = {
            str(item.get("name"))
            for item in catalog
            if isinstance(item, dict) and item.get("allowed") is True
        }
        if "data.profile" not in allowed:
            raise HTTPException(status_code=422, detail="data.profile unavailable")
        if _HYPOTHESIS_MARKER in planning_request and "异常" in planning_request:
            if "stats.anomaly" not in allowed:
                raise HTTPException(status_code=422, detail="stats.anomaly unavailable")
            return json.dumps(
                {
                    "schema_version": 1,
                    "summary": "只验证用户选中的异常候选。",
                    "steps": [
                        {
                            "step_id": "verify_selected_anomaly",
                            "purpose": "取得选中异常候选的 Evidence",
                            "capability": "stats.anomaly",
                            "dependencies": [],
                            "expected_evidence": ["异常检测 Evidence"],
                            "completion_conditions": [
                                "异常工具成功并生成可追溯 Evidence"
                            ],
                            "fallback": [{"when": "异常检测失败", "action": "block"}],
                        }
                    ],
                    "assumptions": [],
                    "clarifications": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if _PARALLEL_MARKER in planning_request:
            if "stats.trend" not in allowed:
                raise HTTPException(status_code=422, detail="stats.trend unavailable")
            return json.dumps(
                {
                    "schema_version": 1,
                    "summary": "并行取得画像与趋势 Evidence。",
                    "steps": [
                        {
                            "step_id": "parallel_profile",
                            "purpose": "取得数据规模与字段画像",
                            "capability": "data.profile",
                            "dependencies": [],
                            "expected_evidence": ["数据画像 Evidence"],
                            "completion_conditions": ["画像工具成功"],
                            "fallback": [{"when": "画像读取失败", "action": "retry"}],
                        },
                        {
                            "step_id": "parallel_trend",
                            "purpose": "取得销售额时间趋势",
                            "capability": "stats.trend",
                            "dependencies": [],
                            "expected_evidence": ["趋势 Evidence"],
                            "completion_conditions": ["趋势工具成功"],
                            "fallback": [{"when": "趋势分析失败", "action": "retry"}],
                        },
                    ],
                    "assumptions": [],
                    "clarifications": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return json.dumps(
            {
                "schema_version": 1,
                "summary": "按父分支反馈重新核对数据画像。",
                "steps": [
                    {
                        "step_id": "profile_feedback_branch",
                        "purpose": "重新核对数据规模与字段画像",
                        "capability": "data.profile",
                        "dependencies": [],
                        "expected_evidence": ["绑定当前分支与数据集版本的画像 Evidence"],
                        "completion_conditions": ["画像工具成功并生成可追溯 Evidence"],
                        "fallback": [{"when": "画像读取失败", "action": "retry"}],
                    }
                ],
                "assumptions": [],
                "clarifications": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    raise HTTPException(status_code=422, detail="unexpected non-stream model call")


def _is_planner_call(messages: list[Any]) -> bool:
    return any(
        isinstance(message, dict)
        and message.get("role") == "system"
        and "受约束任务 Planner" in str(message.get("content", ""))
        for message in messages
    )


def _latest_scenario_marker(messages: list[Any]) -> str | None:
    """Select the active E2E turn without matching markers from older history."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        for marker in (
            _REPORT_MARKER,
            _PARALLEL_MARKER,
            _READ_ONLY_MARKER,
            _BRANCH_MARKER,
            _HYPOTHESIS_MARKER,
        ):
            if marker in content:
                return marker
    return None


def _sse_chunk(
    model: str,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-chatbi-e2e",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n"
    )
