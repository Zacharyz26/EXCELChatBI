"""Deterministic model fixture contracts used by the real Compose browser gate."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from apps.e2e_model.main import (
    _complete_json,
    _latest_scenario_marker,
    _stream_turn,
)
from apps.e2e_model.prepare_fixture import main as prepare_compose_fixture
from mcp_servers.stats.tools import anomaly_detect, trend_analysis
from packages.common.dataset_store import save_dataframe


def test_compose_planner_fixture_returns_a_valid_profile_plan() -> None:
    content = _complete_json(
        [
            {"role": "system", "content": "你是受约束任务 Planner"},
            {
                "role": "user",
                "content": json.dumps(
                    {"capability_catalog": [{"name": "data.profile", "allowed": True}]}
                ),
            },
        ]
    )

    plan = json.loads(content)
    assert plan["schema_version"] == 1
    assert [step["capability"] for step in plan["steps"]] == ["data.profile"]


def test_compose_planner_fixture_returns_two_independent_parallel_steps() -> None:
    content = _complete_json(
        [
            {"role": "system", "content": "你是受约束任务 Planner"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "planning_request": "COMPOSE_6A_PARALLEL：深入分析画像与趋势",
                        "capability_catalog": [
                            {"name": "data.profile", "allowed": True},
                            {"name": "stats.trend", "allowed": True},
                        ],
                    }
                ),
            },
        ]
    )

    plan = json.loads(content)
    assert [step["capability"] for step in plan["steps"]] == [
        "data.profile",
        "stats.trend",
    ]
    assert [step["dependencies"] for step in plan["steps"]] == [[], []]


def test_compose_planner_fixture_binds_the_selected_anomaly_capability() -> None:
    content = _complete_json(
        [
            {"role": "system", "content": "你是受约束任务 Planner"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "planning_request": (
                            "请深入分析这份数据\n\n"
                            "用户对澄清问题的回答：销售额可能存在异常观测"
                        ),
                        "capability_catalog": [
                            {"name": "data.profile", "allowed": True},
                            {"name": "stats.anomaly", "allowed": True},
                            {"name": "stats.trend", "allowed": True},
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    )

    plan = json.loads(content)
    assert plan["summary"] == "只验证用户选中的异常候选。"
    assert [step["capability"] for step in plan["steps"]] == ["stats.anomaly"]
    assert plan["steps"][0]["dependencies"] == []
    assert plan["clarifications"] == []


def test_compose_fixture_selects_the_latest_marked_user_turn() -> None:
    assert (
        _latest_scenario_marker(
            [
                {"role": "user", "content": "COMPOSE_4D_BRANCH"},
                {"role": "assistant", "content": "分支完成"},
                {"role": "user", "content": "COMPOSE_4D_READ_ONLY"},
                {"role": "user", "content": "请按当前计划重试"},
            ]
        )
        == "COMPOSE_4D_READ_ONLY"
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
                {"role": "user", "content": "COMPOSE_6A_PARALLEL"},
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
    trend_call = next(
        call for call in tool_calls if call["function"]["name"] == "trend_analysis"
    )
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
