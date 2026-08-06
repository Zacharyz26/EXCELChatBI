"""Stage 2 observable-behavior evaluation gate tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from packages.models.types import Message, ModelResponse, Scenario, ToolCall
from scripts.stage2_behavior_eval import (
    _load_checkpoint_rows,
    _write_checkpoint,
    evaluate_stage2_report,
)
from scripts.v23_baseline_eval import (
    DEFAULT_CASES,
    _fixture_dataset_ref,
    _FixtureRegistry,
    _run_case,
    load_cases,
)


class _Stage2ScriptedGateway:
    def __init__(self) -> None:
        self.turn = 0

    async def stream_turn(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        tools: list[dict[str, object]] | None = None,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str | ModelResponse]:
        del messages, params
        assert scenario == Scenario.AGENT
        self.turn += 1
        if self.turn == 1:
            assert tools is not None
            text = "我先读取数据画像。"
            yield text
            yield ModelResponse(
                content=text,
                model="stage2-scripted",
                tool_calls=[
                    ToolCall(
                        id="profile-call",
                        name="get_data_profile",
                        arguments=(
                            '{"dataset_ref":"'
                            + _fixture_dataset_ref("B01", "orders")
                            + '"}'
                        ),
                    )
                ],
            )
            return
        text = "这份数据共有 120 行。"
        yield text
        yield ModelResponse(content=text, model="stage2-scripted")

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        del scenario, messages, params
        raise AssertionError("B01 fast plan must not call the LLM Planner")


def _baseline() -> dict[str, object]:
    return {
        "scenario_set_hash": "frozen-cases",
        "metrics": {
            "deepseek-v4-flash": {
                "task_success_rate": 0.26666666666666666,
                "truthful_terminal_rate": 0.36666666666666664,
            }
        },
    }


def _stage2() -> dict[str, object]:
    return {
        "execution_mode": "stage2_structured_plan",
        "scenario_set_hash": "frozen-cases",
        "case_count": 20,
        "repetitions": 3,
        "models": ["deepseek-v4-flash"],
        "rows": [{} for _ in range(60)],
        "metrics": {
            "deepseek-v4-flash": {
                "runs": 60,
                "task_success_rate": 0.6,
                "truthful_terminal_rate": 0.7,
                "forbidden_violations": 0,
                "cost_availability": "available",
            }
        },
    }


def test_stage2_gate_passes_only_with_improvement_and_zero_violations() -> None:
    result = evaluate_stage2_report(_stage2(), _baseline())

    assert result["decision"] == "PASS"
    assert result["full_protocol"] is True
    assert result["blockers"] == []


def test_stage2_gate_rejects_safety_violation() -> None:
    report = _stage2()
    report["metrics"]["deepseek-v4-flash"]["forbidden_violations"] = 1  # type: ignore[index]

    result = evaluate_stage2_report(report, _baseline())

    assert result["decision"] == "NO_GO"
    assert "deepseek-v4-flash:forbidden_violation" in result["blockers"]


def test_stage2_gate_marks_partial_smoke_as_incomplete() -> None:
    report = _stage2()
    report["repetitions"] = 1

    result = evaluate_stage2_report(report, _baseline())

    assert result["decision"] == "INCOMPLETE"
    assert "stage2_full_protocol_incomplete" in result["blockers"]


def test_fixture_registry_exposes_stage2_capability_partition(
    tmp_path: Path,
) -> None:
    registry = _FixtureRegistry("B01", tmp_path)

    catalog = registry.capability_catalog()
    offered = registry.openai_tools_for_capabilities({"data.quality"})

    assert {str(item["name"]) for item in catalog} >= {
        "data.profile",
        "data.quality",
        "dataset.transform",
        "report.generate",
    }
    assert [item["function"]["name"] for item in offered] == ["get_data_profile"]

    remote_catalogs = asyncio.run(registry.validate_remote_catalog())
    snapshot = registry.capability_catalog_snapshot()
    assert snapshot["remote_catalogs"] == [
        {
            "service_name": "fixture-tools",
            "content_hash": remote_catalogs["fixture-tools"],
        }
    ]
    assert registry.validate_capability_catalog_snapshot(snapshot) == ()
    assert registry.tool_names_from_snapshot(snapshot) == frozenset(
        item["function"]["name"] for item in registry.openai_tools()
    )
    assert [
        item["function"]["name"]
        for item in registry.openai_tools(
            allowed_tool_names=frozenset({"kb_search"})
        )
    ] == ["kb_search"]


def test_stage2_mode_executes_persisted_plan_and_scores_observables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apps.orchestrator.agent_loop as agent_loop

    async def direct_call(
        function: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        return function(*args, **kwargs)

    # This unit path owns an isolated temporary SQLite database. Running the
    # small synchronous calls directly avoids pytest/AnyIO worker-loop leakage;
    # the real Stage 2 CLI evaluation covers the threadpool path end to end.
    monkeypatch.setattr(agent_loop, "run_in_threadpool", direct_call)
    case = next(case for case in load_cases(DEFAULT_CASES) if case["id"] == "B01")
    gateway = _Stage2ScriptedGateway()

    row = asyncio.run(
        _run_case(
            case,
            model_name="stage2-scripted",
            repetition=1,
            gateway=gateway,  # type: ignore[arg-type]
            planner_gateway=gateway,  # type: ignore[arg-type]
            enforce_plan=True,
        )
    )

    assert row["planner_route"] == "fast"
    assert row["planned_capabilities"] == ["data.profile"]
    assert row["plan_steps_total"] == 1
    assert row["plan_steps_completed"] == 1
    assert row["task_satisfied"] is True
    assert row["actual_terminal"] == "completed"


def test_stage2_checkpoint_round_trip_and_protocol_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkpoint.json"
    protocol = {
        "schema_version": 1,
        "evaluation": "stage2_structured_agent_observable_behavior",
        "execution_mode": "stage2_structured_plan",
        "scenario_set_hash": "frozen",
        "repetitions": 3,
        "models": ["deepseek-v4-flash"],
        "planner_model": "deepseek-v4-flash",
        "case_ids": ["B01"],
    }
    rows = [
        {
            "case_id": "B01",
            "configured_model": "deepseek-v4-flash",
            "repetition": 1,
        }
    ]

    _write_checkpoint(path, protocol=protocol, rows=rows)

    assert _load_checkpoint_rows(path, expected_protocol=protocol) == rows
    changed = {**protocol, "repetitions": 4}
    with pytest.raises(ValueError, match="协议不匹配"):
        _load_checkpoint_rows(path, expected_protocol=changed)
