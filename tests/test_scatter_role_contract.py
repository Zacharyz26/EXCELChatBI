"""Scatter role preconditions must preserve the real parser/tool axis contract."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.control.data_role_guard import validate_data_role_preconditions
from fastapi.testclient import TestClient
from mcp_servers.chart.tools import gen_chart
from mcp_servers.excel_parser.tools import infer_schema, parse_excel
from packages.models.types import ModelResponse, ToolCall
from packages.session.models import Dataset
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_data_role_guard import _dataset


def _source(tmp_path: Path, kind: str) -> Path:
    dates: list[Any] = list(pd.date_range("2026-01-01", periods=100))
    if kind != "native_xlsx":
        dates = [value.strftime("%Y-%m-%d") for value in dates]
    if kind in {"invalid_iso_csv", "ambiguous_csv", "text_dates_xlsx"}:
        # Keep these text values below the privacy cardinality limit so the
        # actual scatter type check, rather than privacy, decides the outcome.
        dates = ["2026-01-01", "2026-01-02"] * 50
    if kind == "iso_null_csv":
        dates[2] = None
    elif kind == "invalid_iso_csv":
        dates[2] = "2026-02-30"
    elif kind == "ambiguous_csv":
        dates[2] = "03/04/2026"
    frame = pd.DataFrame({
        "发生时间": dates, "金额": list(range(100)), "访问量": list(range(100, 200)),
        "记录ID": list(range(1000, 1100)), "类别": ["A", "B"] * 50,
        "开关": [True, False] * 50,
    })
    source = tmp_path / (kind + (".xlsx" if kind.endswith("xlsx") else ".csv"))
    if source.suffix == ".xlsx":
        frame.to_excel(source, index=False)
    else:
        frame.to_csv(source, index=False, encoding="utf-8-sig")
    return source


def _parsed(source: Path) -> Dataset:
    ref = parse_excel({"file_ref": str(source)})["dataset_ref"]
    profile = infer_schema({"dataset_ref": ref}).to_dict()
    return replace(_dataset(), ref=ref, profile=profile)


def _arguments(dataset: Dataset, column: str, axis: str) -> dict[str, Any]:
    return {
        "dataset_ref": dataset.ref, "chart_type": "scatter",
        "encoding": {"x": column if axis == "x" else "金额",
                     "y": column if axis == "y" else "金额", "agg": "none"},
    }


def _gate(dataset: Dataset, arguments: dict[str, Any]) -> Any:
    return validate_data_role_preconditions(
        tool_name="gen_chart", arguments=arguments, dataset=dataset,
        confirmations=(), data_version_hash="a" * 64,
    )


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("kind", ["native_xlsx", "iso_csv", "iso_null_csv"])
def test_scatter_accepts_parsed_datetime_on_either_axis(tmp_path, axis, kind):
    dataset = _parsed(_source(tmp_path, kind))
    column = next(c for c in dataset.profile["columns"] if c["name"] == "发生时间")
    assert column["dtype"] == "datetime"
    arguments = _arguments(dataset, "发生时间", axis)
    gate = _gate(dataset, arguments)
    assert gate is not None and gate.allowed, gate
    option = gen_chart(arguments)["option"]
    assert option[axis + "Axis"]["type"] == "time"
    assert option[("y" if axis == "x" else "x") + "Axis"]["type"] == "value"
    points = option["series"][0]["data"]
    time_index = 0 if axis == "x" else 1
    assert len(points) == 100
    assert points[0][time_index] == "2026-01-01 00:00:00"
    assert points[-1][time_index] == "2026-04-10 00:00:00"
    assert points[2][time_index] == (None if kind == "iso_null_csv" else "2026-01-03 00:00:00")
    assert [point[1 - time_index] for point in points] == list(range(100))
    (tmp_path / "witness.json").write_text(json.dumps({
        "kind": kind, "axis": axis, "arguments": arguments,
        "gate": gate.evidence(), "option": option,
    }, ensure_ascii=False))


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("kind", ["invalid_iso_csv", "ambiguous_csv", "text_dates_xlsx"])
def test_scatter_does_not_parse_date_labels_or_partly_valid_columns(tmp_path, axis, kind):
    dataset = _parsed(_source(tmp_path, kind))
    column = next(c for c in dataset.profile["columns"] if c["name"] == "发生时间")
    assert column["dtype"] == "str"
    # A semantic time hint is not permission to silently coerce invalid data.
    with pytest.raises(ValueError, match="必须为数值或日期时间类型"):
        gen_chart(_arguments(dataset, "发生时间", axis))


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("column", ["访问量", "记录ID", "类别", "开关"])
def test_scatter_keeps_numeric_support_and_rejects_wrong_roles(tmp_path, axis, column):
    dataset = _parsed(_source(tmp_path, "native_xlsx"))
    arguments = _arguments(dataset, column, axis)
    gate = _gate(dataset, arguments)
    assert gate is not None
    assert gate.allowed is (column == "访问量"), gate
    if gate.allowed:
        option = gen_chart(arguments)["option"]
        assert option["xAxis"]["type"] == option["yAxis"]["type"] == "value"
        assert len(option["series"][0]["data"]) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("kind", ["native_xlsx", "iso_csv", "invalid_iso_csv"])
async def test_real_upload_host_preserves_datetime_contract(tmp_path, axis, kind):
    store = SessionStore(str(tmp_path / "host.db"))
    project = store.create_project("散点图日期契约回归")
    conversation = store.create_conversation(project.id)
    source = _source(tmp_path, kind)
    # API and Host must share the same actual persistent workspace.
    from apps.api.deps import session_store_dep

    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        response = client.post(
            "/upload/excel", data={"project_id": project.id, "conversation_id": conversation.id},
            files={"file": (source.name, source.read_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
    ref = response.json()["dataset_ref"]
    dataset = next(d for d in store.list_datasets(project.id) if d.ref == ref)
    arguments = _arguments(dataset, "发生时间", axis)

    class Gateway:
        turns = 0

        async def stream_turn(self, *args, **kwargs):
            self.turns += 1
            assert self.turns <= 3, "No unbounded scripted repair"
            if self.turns == 1:
                yield ModelResponse(content="", model="offline-scatter-contract", tool_calls=[
                    ToolCall(id="scatter", name="gen_chart", arguments=json.dumps(arguments)),
                ])
            else:
                yield ModelResponse(
                    content="日期列类型不适用，未生成图表。" if kind == "invalid_iso_csv"
                    else "散点图已生成。", model="offline-scatter-contract",
                )

    gateway = Gateway()
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        events = [event async for event in stream_agent_chat(
            conversation_id=conversation.id, project_id=project.id, store=store,
            user_text=f"数据集 {ref}：生成散点图，横轴{arguments['encoding']['x']}，"
                      f"纵轴{arguments['encoding']['y']}，不聚合。",
            gateway=gateway, registry=registry, locks=ConversationLockPool(),
            config=AgentLoopConfig(max_tool_calls=1, max_parallel_tools=1),
        )]
    finally:
        await registry.aclose()
    terminal = [json.loads(e["data"]) for e in events if e["event"] in {"done", "error"}][-1]
    run_id = next(json.loads(e["data"])["run_id"] for e in events if e["event"] == "meta")
    invocations = TaskStore(store.db_path).list_invocations(run_id)
    charts = [a for a in store.list_artifacts(conversation.id) if a.source_tool == "gen_chart"]
    (tmp_path / "events.json").write_text(json.dumps(events, ensure_ascii=False))
    (tmp_path / "witness.json").write_text(json.dumps({
        "kind": kind, "axis": axis, "run_id": run_id, "terminal": terminal,
        "model_fixture_turns": gateway.turns, "paid_calls": 0,
        "invocations": [{"status": item.status, "args": item.args,
                         "error_text": item.error_text} for item in invocations],
        "charts": [{"dataset_ref": chart.dataset_ref, "payload": chart.payload}
                   for chart in charts],
    }, ensure_ascii=False))
    assert len(invocations) == 1
    if kind == "invalid_iso_csv":
        assert terminal.get("run_status") != "completed", terminal
        assert invocations[0].status == "failed"
        assert "必须为数值或日期时间类型" in invocations[0].error_text
        assert charts == []
    else:
        assert terminal.get("run_status") == "completed", terminal
        assert invocations[0].status == "succeeded"
        assert len(charts) == 1 and charts[0].dataset_ref == ref
        assert charts[0].payload["option"][axis + "Axis"]["type"] == "time"
        assert len(charts[0].payload["option"]["series"][0]["data"]) == 100
