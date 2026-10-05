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
_PARALLEL_MARKER = "COMPOSE_6A_PARALLEL"
_HYPOTHESIS_MARKER = "请深入分析这份数据"
_REPORT_MARKER = "COMPOSE_REPORT"
_audit: dict[str, int | bool] = {
    "agent_stream_calls": 0,
    "feedback_marker_seen_in_agent": False,
    "branch_profile_tool_calls": 0,
    "multi_tool_batches": 0,
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
    if payload.get("stream") is True:
        _audit["agent_stream_calls"] = int(_audit["agent_stream_calls"]) + 1
        if _FEEDBACK_MARKER in joined:
            _audit["feedback_marker_seen_in_agent"] = True
        return StreamingResponse(
            _stream_turn(model, messages, payload.get("tools")),
            media_type="text/event-stream",
        )
    raise HTTPException(status_code=422, detail="fixture only supports Agent streaming")


async def _stream_turn(
    model: str,
    messages: list[Any],
    raw_tools: Any,
) -> AsyncIterator[str]:
    call_id = f"call-{uuid.uuid4().hex[:16]}"
    last_message = messages[-1] if messages else None
    has_current_tool_result = isinstance(last_message, dict) and last_message.get("role") == "tool"
    last_tool_name = _tool_result_name(last_message)
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
        _audit["multi_tool_batches"] = int(_audit["multi_tool_batches"]) + 1
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
        _audit["hypothesis_anomaly_tool_calls"] = int(_audit["hypothesis_anomaly_tool_calls"]) + 1
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
    elif (
        scenario_marker == _REPORT_MARKER
        and not has_current_tool_result
        and "get_data_profile" in tool_names
        and not _report_analysis_ids(messages, artifact_type="profile")
    ):
        dataset_refs = re.findall(r"最新数据集 ([0-9a-f]{32})", joined)
        if not dataset_refs:
            raise HTTPException(status_code=422, detail="dataset_ref missing")
        yield _sse_chunk(
            model,
            {
                "role": "assistant",
                "content": "我会先生成本次报告所需的数据画像 Evidence。",
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
                            "name": "get_data_profile",
                            "arguments": json.dumps(
                                {"dataset_ref": dataset_refs[-1]},
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
        scenario_marker == _REPORT_MARKER
        and "generate_report" in tool_names
        and (not has_current_tool_result or last_tool_name == "get_data_profile")
    ):
        analysis_ids = _report_analysis_ids(messages)
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
                "analysis_ids": analysis_ids,
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


def _report_analysis_ids(
    messages: list[Any],
    *,
    artifact_type: str | None = None,
) -> list[str]:
    """Select typed report inputs from the Host artifact registry, not recency alone."""
    joined = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict)
    )
    selected: list[str] = []
    for analysis_id, current_type in re.findall(
        r"\[analysis_id=([A-Za-z0-9_-]+)\][^\n]*?类型=(profile|stats|chart|table)",
        joined,
    ):
        if artifact_type is not None and current_type != artifact_type:
            continue
        if analysis_id not in selected:
            selected.append(analysis_id)
    return selected


def _tool_result_name(message: Any) -> str | None:
    if not isinstance(message, dict) or message.get("role") != "tool":
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    tool = payload.get("tool") if isinstance(payload, dict) else None
    return tool if isinstance(tool, str) else None


def _latest_scenario_marker(messages: list[Any]) -> str | None:
    """Select the active E2E turn without matching markers from older history."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        for marker in (
            _REPORT_MARKER,
            _PARALLEL_MARKER,
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
