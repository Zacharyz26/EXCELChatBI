"""Independent R3 surface invariants on real tools, SQLite and completion gates.

The model alone is scripted. CSV arithmetic and explicit semantic changes are
the oracle; neither the parser nor the verifier defines expected outcomes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import socket
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from sse_starlette.sse import AppStatus

from tests.test_agent_tools import _registry
from tests.test_r1_core_real_paths import real_host

ROOT = Path(__file__).resolve().parents[1]
FIELD = "播出时长（分钟）"
PROFILE_GOAL = "请查看当前数据画像，暂不筛选或清洗。"
FORMATS = {
    "plain": FIELD + "最大值为{value}。",
    "bold": "**" + FIELD + "**的最大值为**{value}**。",
    "inline-code": "`" + FIELD + "`的最大值为`{value}`。",
    "heading": "## " + FIELD + "\n\n该列最大值为{value}。",
    "bullet": "- " + FIELD + "最大值为{value}。",
    "table": "| 字段 | 最大值 |\n| --- | --- |\n| " + FIELD + " | {value} |",
    "quoted-assignment": FIELD + '最大值为"{value}"。',
    "soft-line": FIELD + "最大值为\n{value}。",
}


@pytest.fixture(autouse=True)
def isolated_offline_runtime(monkeypatch, tmp_path):
    def deny(*_args, **_kwargs):
        raise AssertionError("R3 real-path tests cannot contact an external model or service")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    AppStatus.should_exit_event = None
    try:
        yield
    finally:
        get_settings.cache_clear()
        AppStatus.should_exit_event = None


def independent_oracle():
    body = (ROOT / "examples/handoff/broadcast.csv").read_bytes()
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    values = [float(row[FIELD]) for row in rows if row[FIELD].strip()]
    filtered = [row for row in rows if row["频道"] == "A" and row["状态"] == "已播出"]
    oracle = {
        "input_sha256": hashlib.sha256(body).hexdigest(),
        "rows": len(rows),
        "filtered": len(filtered),
        "non_null": len(values),
        "null": len(rows) - len(values),
        "max": max(values),
        "min": min(values),
    }
    assert oracle["rows"] == 75 and oracle["filtered"] == 38
    assert oracle["max"] == 999 and oracle["min"] == 10
    assert oracle["null"] == 1 and oracle["non_null"] == 74
    return oracle


async def check_report_and_persistence(tmp_path, result, answer, expected):
    """Call the actual report tool; this is not an Agent/report-browser claim."""
    run = result["run"]
    store = result["store"]
    selected = [a for a in result["artifacts"] if a.source_tool == "get_data_profile"]
    assert len(selected) == 1
    registry = _registry(
        AgentContext(store, run.project_id, run.conversation_id, run_id=run.run_id)
    )
    arguments = {
        "analysis_ids": [selected[0].params["analysis_id"]],
        "title": "独立格式核验",
        "insights": answer,
        "include_pdf": False,
    }
    report_witness = {"arguments": arguments, "expected": expected, "pdf": "NOT_RUN"}
    try:
        if not expected:
            with pytest.raises(AgentToolError) as error:
                registry.execute("generate_report", json.dumps(arguments, ensure_ascii=False))
            report_witness["actual"] = "REJECT"
            report_witness["error"] = str(error.value)
        else:
            report = registry.execute("generate_report", json.dumps(arguments, ensure_ascii=False))
            content = Path(report["md_path"]).read_bytes()
            assert answer in content.decode("utf-8")
            assert report["validation"]["status"] == "passed"
            assert report["validation"]["content_sha256"] == hashlib.sha256(content).hexdigest()
            report_witness["actual"] = "PASS"
            report_witness["result"] = report
            (tmp_path / "generated-report.md").write_bytes(content)
    finally:
        await registry.aclose()
        (tmp_path / "report-witness.json").write_text(
            json.dumps(report_witness, ensure_ascii=False, indent=2)
        )
    reopened = SessionStore(str(store.db_path))
    restored = TaskStore(reopened.db_path).get_run(run.run_id)
    assert restored is not None and restored.status == run.status
    restored_artifact = next(
        a for a in reopened.list_artifacts(run.conversation_id) if a.id == selected[0].id
    )
    assert restored_artifact.payload == selected[0].payload
    assert restored_artifact.dataset_ref == selected[0].dataset_ref


@pytest.mark.asyncio
@pytest.mark.parametrize("format_name", FORMATS)
@pytest.mark.parametrize("expected,value", [(True, 999), (False, 888)])
async def test_profile_formats_real_host_and_report(tmp_path, format_name, expected, value):
    oracle = independent_oracle()
    answer = FORMATS[format_name].format(value=value)
    result = await real_host(
        tmp_path, tool="get_data_profile", operation={}, goal=PROFILE_GOAL, answer=answer
    )
    (tmp_path / "oracle.json").write_text(json.dumps(oracle, ensure_ascii=False, indent=2))
    refs = [ref for claim in result["claims"] for ref in claim.value_refs]
    assert any(str(value) in str(ref.get("token", "")) for ref in refs), result["witness"]
    assert (result["run"].status == "completed") is expected, result["witness"]
    if expected:
        assert any(
            ref.get("supported") and ref.get("semantic_binding", {}).get("statistic") == "max"
            for ref in refs
        ), refs
    else:
        assert any(not ref.get("supported") for ref in refs), refs
    await check_report_and_persistence(tmp_path, result, answer, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("formatted", [False, True])
@pytest.mark.parametrize("expected", [False, True])
async def test_filter_roles_real_host_and_report(tmp_path, formatted, expected):
    oracle = independent_oracle()
    before, after = (75, 38) if expected else (38, 75)
    answer = f"处理结果：筛选前{before}行，筛选后{after}行。"
    if formatted:
        answer = f"处理结果：筛选前 **{before}** 行，筛选后 **{after}** 行。"
    result = await real_host(
        tmp_path,
        tool="transform_dataset",
        operation={
            "filters": [
                {"column": "频道", "op": "==", "value": "A"},
                {"column": "状态", "op": "==", "value": "已播出"},
            ]
        },
        goal="筛选条件：频道=A，状态=已播出。说明筛选前和筛选后的行数。",
        answer=answer,
        follow_profile=True,
    )
    transform = next(
        record for record in result["records"] if record.source.get("tool") == "transform_dataset"
    )
    projection = transform.summary["fact_projection"]
    child = result["store"].get_dataset(projection["output_ref"])
    assert child is not None and child.parent_ref == result["source_ref"]
    assert len(load_dataframe(child.ref)) == oracle["filtered"]
    assert len(load_dataframe(result["source_ref"])) == oracle["rows"]
    assert projection["input_ref"] == result["source_ref"]
    assert (result["run"].status == "completed") is expected, result["witness"]
    report_answer = f"当前数据共{'38' if expected else '75'}行。"
    if formatted:
        report_answer = f"当前数据共 **{'38' if expected else '75'}** 行。"
    await check_report_and_persistence(tmp_path, result, report_answer, expected)
    (tmp_path / "independent-transform.json").write_text(
        json.dumps(
            {
                "oracle": oracle,
                "child": asdict(child),
                "projection": projection,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected",
    [
        ('标签为"最大值"和"最小值"。' + FIELD + "最大值为999。", True),
        ("## " + FIELD + "\n\n该列最大值为999。\n\n## 记录ID\n\n该列最大值为999。", False),
    ],
)
async def test_label_and_field_boundaries_real_host(tmp_path, answer, expected):
    independent_oracle()
    result = await real_host(
        tmp_path, tool="get_data_profile", operation={}, goal=PROFILE_GOAL, answer=answer
    )
    assert (result["run"].status == "completed") is expected, result["witness"]
    await check_report_and_persistence(tmp_path, result, answer, expected)
