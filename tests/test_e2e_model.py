"""Deterministic model fixture contracts used by the real Compose browser gate."""

from __future__ import annotations

import json

import pytest
from apps.e2e_model.main import (
    _complete_json,
    _latest_scenario_marker,
    _stream_turn,
)


def test_compose_planner_fixture_returns_a_valid_profile_plan() -> None:
    content = _complete_json(
        [
            {"role": "system", "content": "你是受约束任务 Planner"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "capability_catalog": [
                            {"name": "data.profile", "allowed": True}
                        ]
                    }
                ),
            },
        ]
    )

    plan = json.loads(content)
    assert plan["schema_version"] == 1
    assert [step["capability"] for step in plan["steps"]] == ["data.profile"]


def test_compose_fixture_selects_the_latest_marked_user_turn() -> None:
    assert _latest_scenario_marker(
        [
            {"role": "user", "content": "COMPOSE_4D_BRANCH"},
            {"role": "assistant", "content": "分支完成"},
            {"role": "user", "content": "COMPOSE_4D_READ_ONLY"},
            {"role": "user", "content": "请按当前计划重试"},
        ]
    ) == "COMPOSE_4D_READ_ONLY"


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
    assert json.loads(tool_call["function"]["arguments"]) == {
        "dataset_ref": dataset_ref
    }
    assert body.endswith("data: [DONE]\n\n")
