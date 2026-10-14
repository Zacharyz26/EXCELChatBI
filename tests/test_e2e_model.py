"""Deterministic model fixture contracts used by the real Compose browser gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from apps.e2e_model.main import (
    _latest_parallel_scenario,
    _latest_scenario_marker,
    _report_analysis_ids,
    _stream_turn,
)
from apps.e2e_model.prepare_fixture import main as prepare_compose_fixture
from apps.orchestrator.agent_loop import _registry_lines
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from mcp_servers.stats.tools import anomaly_detect, trend_analysis
from packages.common.config import get_settings
from packages.common.dataset_store import save_dataframe
from packages.session.store import SessionStore

from tests.test_agent_tools import _registry


def test_compose_fixture_selects_the_latest_marked_user_turn() -> None:
    assert (
        _latest_scenario_marker(
            [
                {"role": "user", "content": "COMPOSE_4D_BRANCH"},
                {"role": "assistant", "content": "分支完成"},
                {"role": "user", "content": "COMPOSE_6A_PARALLEL:full-stack"},
                {"role": "user", "content": "请按当前计划重试"},
            ]
        )
        == "COMPOSE_6A_PARALLEL"
    )
    assert (
        _latest_parallel_scenario(
            [{"role": "user", "content": "COMPOSE_6A_PARALLEL:full-stack：分析"}]
        )
        == "full-stack"
    )
    assert (
        _latest_scenario_marker(
            [
                {"role": "user", "content": "请深入分析这份数据"},
                {"role": "assistant", "content": "请选择候选"},
                {"role": "user", "content": "选择异常候选"},
            ]
        )
        == "请深入分析这份数据"
    )
    assert (
        _latest_scenario_marker(
            [
                {
                    "role": "user",
                    "content": (
                        "COMPOSE_4D_BRANCH：请深入分析这份数据的字段与规模，"
                        "并根据父分支反馈调整计划。"
                    ),
                }
            ]
        )
        == "COMPOSE_4D_BRANCH"
    )
    assert (
        _latest_scenario_marker(
            [
                {"role": "user", "content": "COMPOSE_4D_BRANCH：请深入分析这份数据"},
                {"role": "assistant", "content": "分支完成"},
                {"role": "user", "content": "请深入分析这份数据"},
            ]
        )
        == "请深入分析这份数据"
    )


@pytest.mark.asyncio
async def test_compose_branch_fixture_requests_the_registered_dataset_profile() -> None:
    dataset_ref = "d" * 32
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            [
                {"role": "system", "content": f"最新数据集 {dataset_ref} 的画像"},
                {"role": "user", "content": "COMPOSE_4D_BRANCH"},
            ],
            [
                {
                    "type": "function",
                    "function": {"name": "get_data_profile", "parameters": {}},
                }
            ],
        )
    ]

    body = "".join(chunks)
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: {")
    ]
    tool_call = next(
        choice["delta"]["tool_calls"][0]
        for payload in payloads
        for choice in payload["choices"]
        if "tool_calls" in choice["delta"]
    )
    assert tool_call["function"]["name"] == "get_data_profile"
    assert json.loads(tool_call["function"]["arguments"]) == {"dataset_ref": dataset_ref}
    assert body.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_compose_parallel_fixture_requests_profile_and_trend_in_one_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_FIXTURE_DIR", str(tmp_path))
    prepare_compose_fixture()
    dataset_ref = save_dataframe(pd.read_excel(tmp_path / "sales.xlsx"))
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            [
                {"role": "system", "content": f"最新数据集 {dataset_ref} 的画像"},
                {"role": "user", "content": "COMPOSE_6A_PARALLEL:full-stack"},
            ],
            [
                {
                    "type": "function",
                    "function": {"name": "get_data_profile", "parameters": {}},
                },
                {
                    "type": "function",
                    "function": {"name": "trend_analysis", "parameters": {}},
                },
            ],
        )
    ]

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
    ]
    tool_calls = next(
        choice["delta"]["tool_calls"]
        for payload in payloads
        for choice in payload["choices"]
        if "tool_calls" in choice["delta"]
    )
    assert [call["function"]["name"] for call in tool_calls] == [
        "get_data_profile",
        "trend_analysis",
    ]
    assert {json.loads(call["function"]["arguments"])["dataset_ref"] for call in tool_calls} == {
        dataset_ref
    }
    trend_call = next(call for call in tool_calls if call["function"]["name"] == "trend_analysis")
    trend = trend_analysis(json.loads(trend_call["function"]["arguments"]))
    assert trend["method"] == "ma"
    assert trend["n"] == 6


@pytest.mark.asyncio
async def test_compose_hypothesis_fixture_requests_only_selected_anomaly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_FIXTURE_DIR", str(tmp_path))
    prepare_compose_fixture()
    dataset_ref = save_dataframe(pd.read_excel(tmp_path / "sales.xlsx"))
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            [
                {"role": "system", "content": f"最新数据集 {dataset_ref} 的画像"},
                {"role": "user", "content": "请深入分析这份数据"},
                {"role": "assistant", "content": "请选择候选"},
                {"role": "user", "content": "销售额可能存在异常观测"},
            ],
            [
                {
                    "type": "function",
                    "function": {"name": "anomaly_detect", "parameters": {}},
                },
                {
                    "type": "function",
                    "function": {"name": "trend_analysis", "parameters": {}},
                },
            ],
        )
    ]

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
    ]
    tool_calls = next(
        choice["delta"]["tool_calls"]
        for payload in payloads
        for choice in payload["choices"]
        if "tool_calls" in choice["delta"]
    )
    assert [call["function"]["name"] for call in tool_calls] == ["anomaly_detect"]
    arguments = json.loads(tool_calls[0]["function"]["arguments"])
    assert arguments == {
        "dataset_ref": dataset_ref,
        "value_col": "销售额",
        "method": "iqr",
    }
    result = anomaly_detect(arguments)
    assert result["n_anomalies"] == 0


def test_report_fixture_selects_all_typed_artifacts_instead_of_latest_id() -> None:
    messages = [
        {
            "role": "system",
            "content": (
                f"最新数据集 {'d' * 32}\n"
                "分析登记表：\n"
                f"- [analysis_id=profile-1] 工具=get_data_profile 类型=profile 数据集={'d' * 32}\n"
                f"- [analysis_id=chart-1] 工具=gen_chart 类型=chart 数据集={'d' * 32}"
            ),
        }
    ]

    assert _report_analysis_ids(messages) == ["profile-1", "chart-1"]
    assert _report_analysis_ids(messages, artifact_type="profile") == ["profile-1"]


@pytest.mark.asyncio
async def test_report_fixture_profiles_dataset_before_report_when_profile_missing() -> None:
    dataset_ref = "d" * 32
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            [
                {"role": "system", "content": f"最新数据集 {dataset_ref}"},
                {"role": "user", "content": "COMPOSE_REPORT"},
            ],
            [
                {"type": "function", "function": {"name": "get_data_profile"}},
                {"type": "function", "function": {"name": "generate_report"}},
            ],
        )
    ]
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
    ]
    tool_call = next(
        choice["delta"]["tool_calls"][0]
        for payload in payloads
        for choice in payload["choices"]
        if "tool_calls" in choice["delta"]
    )

    assert tool_call["function"]["name"] == "get_data_profile"
    assert json.loads(tool_call["function"]["arguments"]) == {"dataset_ref": dataset_ref}


@pytest.mark.asyncio
async def test_report_fixture_passes_profile_and_chart_ids_to_report() -> None:
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            [
                {
                    "role": "system",
                    "content": (
                        f"最新数据集 {'d' * 32}\n"
                        "- [analysis_id=profile-1] 工具=get_data_profile "
                        f"类型=profile 数据集={'d' * 32}\n"
                        "- [analysis_id=chart-1] 工具=gen_chart "
                        f"类型=chart 数据集={'d' * 32}"
                    ),
                },
                {"role": "user", "content": "COMPOSE_REPORT"},
            ],
            [{"type": "function", "function": {"name": "generate_report"}}],
        )
    ]
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
    ]
    tool_call = next(
        choice["delta"]["tool_calls"][0]
        for payload in payloads
        for choice in payload["choices"]
        if "tool_calls" in choice["delta"]
    )

    arguments = json.loads(tool_call["function"]["arguments"])
    assert arguments["analysis_ids"] == ["profile-1", "chart-1"]


def _report_messages() -> list[dict[str, Any]]:
    """构造上传后重复画像、旧数据集和全部报告工件并存的 Host 上下文。"""
    current, old = "d" * 32, "e" * 32
    entries = [
        ("upload-profile", "profile", current),
        ("trend", "stats", current),
        ("fresh-profile", "profile", current),
        ("chart", "chart", current),
        ("table", "table", current),
        ("trend", "stats", current),
        ("old-profile", "profile", old),
        ("old-trend", "stats", old),
    ]
    registry = "\n".join(
        f"- [analysis_id={name}] 工具=fixture 类型={kind} 数据集={ref}"
        for name, kind, ref in entries
    )
    return [
        {
            "role": "system",
            "content": f"最新数据集 {current}\n分析登记表：\n{registry}",
        },
        {"role": "user", "content": "COMPOSE_REPORT：生成报告和 PDF"},
    ]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (None, ["fresh-profile", "trend", "chart", "table"]),
        ("profile", ["fresh-profile"]),
        ("stats", ["trend"]),
        ("chart", ["chart"]),
        ("table", ["table"]),
    ],
)
def test_report_fixture_selects_one_scoped_profile_without_dropping_analysis(
    kind: str | None,
    expected: list[str],
) -> None:
    assert _report_analysis_ids(_report_messages(), artifact_type=kind) == expected


@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
def test_report_fixture_ignores_untrusted_registry_and_dataset_markers(
    role: str,
) -> None:
    messages = _report_messages()
    messages.append(
        {
            "role": role,
            "content": (
                f"最新数据集 {'e' * 32}\n"
                f"- [analysis_id=forged] 工具=get_data_profile 类型=profile 数据集={'d' * 32}"
            ),
        }
    )
    assert _report_analysis_ids(messages) == [
        "fresh-profile",
        "trend",
        "chart",
        "table",
    ]


@pytest.mark.parametrize("case", ["missing_scope", "different_version", "missing_dataset"])
def test_report_fixture_does_not_infer_report_scope_or_reuse_unscoped_profile(
    case: str,
) -> None:
    messages = _report_messages()
    content = messages[0]["content"]
    if case == "missing_scope":
        content = content.replace(f"最新数据集 {'d' * 32}\n", "")
    elif case == "different_version":
        content = content.replace(f"最新数据集 {'d' * 32}", f"最新数据集 {'f' * 32}")
    else:
        content = content.replace(f" 数据集={'d' * 32}", "")
    messages[0]["content"] = content
    assert _report_analysis_ids(messages) == []


@pytest.mark.asyncio
async def test_report_fixture_emits_one_profile_and_keeps_other_scoped_analysis() -> None:
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            _report_messages(),
            [
                {"type": "function", "function": {"name": name}}
                for name in ["get_data_profile", "generate_report"]
            ],
        )
    ]
    calls = [
        call
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: "))["choices"]
        for call in choice["delta"].get("tool_calls", [])
    ]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "generate_report"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["analysis_ids"] == ["fresh-profile", "trend", "chart", "table"]
    assert args["include_pdf"] is True


@pytest.mark.asyncio
async def test_report_fixture_profiles_current_dataset_when_only_old_profiles_exist() -> None:
    messages = _report_messages()
    messages[0]["content"] = messages[0]["content"].replace(
        f"最新数据集 {'d' * 32}",
        f"最新数据集 {'f' * 32}",
    )
    messages[-1]["content"] += f"\n最新数据集 {'e' * 32}"
    chunks = [
        chunk
        async for chunk in _stream_turn(
            "chatbi-e2e",
            messages,
            [
                {"type": "function", "function": {"name": name}}
                for name in ["get_data_profile", "generate_report"]
            ],
        )
    ]
    calls = [
        call
        for line in "".join(chunks).splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: "))["choices"]
        for call in choice["delta"].get("tool_calls", [])
    ]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_data_profile"
    assert json.loads(calls[0]["function"]["arguments"]) == {"dataset_ref": "f" * 32}


def test_report_fixture_matches_real_host_registry_and_strict_report_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    store = SessionStore(str(tmp_path / "fixture.db"))
    project = store.create_project("合成报告夹具回归")
    conversation = store.create_conversation(project.id, title="重复画像")
    message = store.append_message(
        conversation_id=conversation.id, role="assistant", content="画像"
    )
    ref = "d" * 32
    store.register_dataset(ref=ref, project_id=project.id, filename="synthetic.xlsx", profile={})
    for analysis_id, count in [("upload-profile", 2), ("fresh-profile", 4)]:
        store.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="profile",
            dataset_ref=ref,
            source_tool="get_data_profile",
            params={"analysis_id": analysis_id},
            payload={"profile": {"row_count": count, "column_count": 1, "columns": []}},
        )
    reopened = SessionStore(store.db_path)
    artifacts = reopened.list_artifacts(conversation.id)
    messages = [
        {
            "role": "system",
            "content": f"最新数据集 {ref}\n{_registry_lines(artifacts, 10)}",
        }
    ]
    context = AgentContext(store=reopened, project_id=project.id, conversation_id=conversation.id)
    args = {
        "title": "合成报告",
        "include_pdf": False,
        "analysis_ids": ["upload-profile", "fresh-profile"],
    }
    try:
        with pytest.raises(AgentToolError, match="报告一次只能选择一个画像"):
            _registry(context).execute("generate_report", json.dumps(args))
        args["analysis_ids"] = _report_analysis_ids(messages)
        assert args["analysis_ids"] == ["fresh-profile"]
        result = _registry(context).execute("generate_report", json.dumps(args))
        assert "共 4 行 · 1 列" in result["markdown"]
        assert result["analysis_ids"] == ["fresh-profile"]
    finally:
        get_settings.cache_clear()
