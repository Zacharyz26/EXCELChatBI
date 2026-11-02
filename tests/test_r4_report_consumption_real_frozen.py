"""Independent R4 report-consumer safety matrix, NOT_RUN until root execution.

Only model decisions are scripted. Sources come from actual upload, aggregate,
chart, Host, TaskStore and SQLite; no hand-written success/Evidence/Artifact.
All mutations affect this test's owned tmp files. No provider or real ledger.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from apps.orchestrator.agent_loop import _restore_task_contract
from apps.orchestrator.control.report_delivery import verified_report_totals
from apps.orchestrator.control.verifier import verify_completion
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe
from packages.session.task_store import TaskStore

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_local_chain_draft import api_request, artifact_id, conversation_detail

METRIC = "播出时长（分钟）"
ORIGINAL = "用当前已生成的月度汇总表和折线图生成新的 PDF 报告，保留月度数据、总计及单位。"
ANSWER = "已生成新的分析报告。"


def synthetic_csv():
    lines = [f"月份,频道,状态,{METRIC}"]
    for month, start in (("2026-01", 1), ("2026-02", 7)):
        for offset in range(6):
            lines.append(f"{month},{'B' if offset == 5 else 'A'},已播出,{start + offset}")
    body = ("\n".join(lines) + "\n").encode()
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    monthly = {}
    for row in rows:
        monthly[row["月份"]] = monthly.get(row["月份"], Decimal(0)) + Decimal(row[METRIC])
    assert len(rows) == 12 and monthly == {"2026-01": Decimal(21), "2026-02": Decimal(57)}
    assert sum(monthly.values()) == 78
    # Both groups meet the unchanged min-size5 policy; no empty-chart shortcut.
    assert all(sum(r["月份"] == m for r in rows) == 6 for m in monthly)
    return body, rows, monthly


def succeeded(result, tool):
    return [
        x for x in result["invocations"] if x["tool_name"] == tool and x["status"] == "succeeded"
    ]


async def one_tool(store, project, conversation, tmp_path, label, tool, args, goal, answer):
    model = support.ScriptedModel([(tool, args)], answer)
    result = await support.execute(store, project, conversation, model, goal)
    support.write_witness(tmp_path, label, goal=goal, model_messages=model.messages, result=result)
    assert result["run"]["status"] == "completed", result
    calls = succeeded(result, tool)
    assert len(calls) == 1, result
    return result, calls[0]


@pytest_asyncio.fixture
async def actual_sources(tmp_path):
    body, rows, monthly = synthetic_csv()
    store, project, conversation = support.workspace(tmp_path)
    uploaded = await api_request(
        store,
        "POST",
        "/upload/excel",
        data={"project_id": project.id, "conversation_id": conversation.id},
        files={"file": ("report-source-only-synthetic.csv", body, "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    ref = uploaded.json()["dataset_ref"]
    assert len(load_dataframe(ref)) == 12
    aggregate, agg_call = await one_tool(
        store,
        project,
        conversation,
        tmp_path,
        "setup-real-aggregate",
        "aggregate_preview",
        {"dataset_ref": ref, "group_col": "月份", "value_col": METRIC, "agg": "sum"},
        f"dataset_ref={ref}；按月份汇总{METRIC}并给出总计。",
        f"{METRIC}总计为78。",
    )
    chart_run, chart_call = await one_tool(
        store,
        project,
        conversation,
        tmp_path,
        "setup-real-chart",
        "gen_chart",
        {
            "dataset_ref": ref,
            "chart_type": "line",
            "encoding": {"x": "月份", "y": METRIC, "agg": "sum"},
        },
        f"请为当前数据生成按月份展示{METRIC}的折线图。",
        "已生成当前数据的月度播出时长折线图。",
    )
    sources = store.list_artifacts(conversation.id)
    table = next(a for a in sources if a.id == agg_call["artifact_id"])
    chart = next(a for a in sources if a.id == chart_call["artifact_id"])
    assert table.dataset_ref == chart.dataset_ref == ref
    assert Decimal(str(table.payload["total_value"])) == Decimal(78)
    # R4-REPORT-FIXTURE-ORDER-01: no sort was requested; the actual tool's
    # default is value_desc. Group identity and exact value, not list position,
    # define this independent aggregate oracle. Chart chronology remains below.
    assert len(table.payload["rows"]) == len(monthly)
    assert {
        str(row["group"]): Decimal(str(row["value"])) for row in table.payload["rows"]
    } == monthly
    option = chart.payload["option"]
    assert option["xAxis"]["data"] == list(monthly)
    assert [Decimal(str(x)) for x in option["series"][0]["data"]] == list(monthly.values())
    detail = await conversation_detail(store, conversation)
    projection = support.execution_projection(detail)
    ids = [artifact_id(table), artifact_id(chart)]
    assert projection["availability"] == "available" and projection["dataset_ref"] == ref
    assert set(ids).issubset(projection["analysis_ids"])
    support.write_witness(
        tmp_path,
        "setup-source-oracle-and-proof",
        input_sha256=hashlib.sha256(body).hexdigest(),
        rows=rows,
        monthly=monthly,
        current_projection=projection,
        table=asdict(table),
        chart=asdict(chart),
        aggregate_run=aggregate["run"],
        chart_run=chart_run["run"],
    )
    return {
        "store": store,
        "project": project,
        "conversation": conversation,
        "ref": ref,
        "table": table,
        "chart": chart,
        "ids": ids,
    }


async def report_attempt(source, tmp_path, *, goal=ORIGINAL, ids=None):
    chosen = source["ids"] if ids is None else ids
    model = support.ScriptedModel(
        [
            (
                "generate_report",
                {"analysis_ids": chosen, "title": "合成分析报告", "include_pdf": True},
            ),
        ],
        ANSWER,
    )
    result = await support.execute(
        source["store"], source["project"], source["conversation"], model, goal
    )
    support.write_witness(
        tmp_path,
        "report-attempt",
        goal=goal,
        chosen_analysis_ids=chosen,
        model_messages=model.messages,
        result=result,
    )
    return result


def full_verdict(source, result):
    store, conversation = source["store"], source["conversation"]
    tasks = TaskStore(store.db_path)
    run_id = result["run"]["run_id"]
    contract = _restore_task_contract(tasks.get_verified_contract(run_id), run_id)
    invocations, evidence = tasks.list_invocations(run_id), tasks.list_evidence(run_id)
    report_ids = {x.artifact_id for x in invocations if x.tool_name == "generate_report"}
    all_artifacts = store.list_artifacts(conversation.id)
    produced = [a for a in all_artifacts if a.id in report_ids]
    verdict = verify_completion(
        contract=contract,
        final_text=ANSWER,
        artifacts=produced,
        source_artifacts=[a for a in all_artifacts if a.id not in report_ids],
        invocations=invocations,
        evidence=evidence,
        dataset_bindings=tasks.list_dataset_bindings(run_id),
        plan_steps=tasks.list_plan_steps(run_id),
    )
    return verdict, produced, contract, invocations, evidence, all_artifacts


@pytest.mark.asyncio
async def test_original_consumes_real_current_sources_and_delivers_new_report(
    actual_sources, tmp_path
):
    result = await report_attempt(actual_sources, tmp_path)
    assert result["model_calls"] > 0, result  # Not an early, unrelated clarification.
    assert result["run"]["status"] == "completed", result
    assert len(succeeded(result, "generate_report")) == 1
    assert not succeeded(result, "aggregate_preview") and not succeeded(result, "gen_chart")
    verdict, reports, contract, calls, evidence, sources = full_verdict(actual_sources, result)
    support.write_witness(
        tmp_path,
        "actual-report-full-verifier",
        verdict=asdict(verdict),
        reports=[asdict(x) for x in reports],
    )
    assert verdict.passed, verdict
    assert (
        contract.analysis_requirements.total
        and contract.analysis_requirements.total_column == METRIC
    )
    assert len(reports) == 1
    report = reports[0]
    assert set(report.params["analysis_ids"]) == set(actual_sources["ids"])
    pdf, markdown = Path(report.file_ref), Path(report.file_ref).with_suffix(".md")
    assert pdf.read_bytes().startswith(b"%PDF-")
    text = markdown.read_text()
    assert "![chart](data:image/png;base64," in text
    assert "完整数据总计：78" in text and METRIC in text
    assert all(month in text for month in ("2026-01", "2026-02"))
    validation = report.payload["validation"]
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == validation["pdf_sha256"]
    assert hashlib.sha256(markdown.read_bytes()).hexdigest() == validation["content_sha256"]
    call = next(x for x in calls if x.tool_name == "generate_report" and x.status == "succeeded")
    ev = next(x for x in evidence if x.invocation_id == call.invocation_id)
    totals = verified_report_totals(report, call, ev, sources)
    assert len(totals) == 1 and Decimal(str(totals[0]["value"])) == 78


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal,required_new_aggregate,required_new_chart",
    [
        (f"重新按月份汇总{METRIC}，计算总计，生成折线图并生成新的 PDF 报告。", True, True),
        ("使用已有的月度汇总表，重新生成折线图，再生成新的 PDF 报告。", False, True),
        (f"使用已有的折线图，重新按月份汇总{METRIC}并计算总计，生成新的 PDF 报告。", True, False),
    ],
    ids=["recompute-and-chart", "new-chart-only", "recompute-only"],
)
async def test_consuming_old_sources_cannot_satisfy_new_operations(
    actual_sources, tmp_path, goal, required_new_aggregate, required_new_chart
):
    result = await report_attempt(actual_sources, tmp_path, goal=goal)
    req = result["contract"]["analysis_requirements"]
    active_aggregate = [
        x for x in req["operations"] if x["kind"] == "aggregate" and x["disposition"] == "current"
    ]
    assert bool(active_aggregate) is required_new_aggregate, result
    chart_required = any(
        x["artifact_type"] == "chart" and x["required"]
        for x in result["contract"]["success_criteria"]
    )
    assert chart_required is required_new_chart, result
    assert not req["clarifications"], result
    assert result["model_calls"] > 0, result
    assert result["run"]["status"] != "completed", result
    assert not succeeded(result, "aggregate_preview") and not succeeded(result, "gen_chart")


@pytest.mark.asyncio
async def test_table_only_report_does_not_deliver_requested_existing_chart(
    actual_sources, tmp_path
):
    result = await report_attempt(
        actual_sources, tmp_path, ids=[artifact_id(actual_sources["table"])]
    )
    assert not result["contract"]["analysis_requirements"]["clarifications"], result
    assert result["model_calls"] > 0, result
    assert result["run"]["status"] != "completed", result
    # A report may be rejected before assembly or after real table-only output;
    # either must leave the explicitly requested chart delivery unfulfilled.


@pytest.mark.asyncio
async def test_parent_artifacts_cannot_replace_current_filtered_sources(actual_sources, tmp_path):
    source = actual_sources
    filtered, _ = await one_tool(
        source["store"],
        source["project"],
        source["conversation"],
        tmp_path,
        "later-real-filter",
        "transform_dataset",
        {"dataset_ref": source["ref"], "filters": [{"column": "频道", "op": "==", "value": "A"}]},
        f"dataset_ref={source['ref']}；筛选条件：频道=A。说明筛选前后的行数。",
        "筛选前12行，筛选后10行。",
    )
    record = next(x for x in filtered["evidence"] if x["source"].get("tool") == "transform_dataset")
    child = record["summary"]["fact_projection"]["output_ref"]
    assert child != source["ref"] and len(load_dataframe(child)) == 10
    assert Decimal(str(load_dataframe(child)[METRIC].sum())) == 60
    projection = support.execution_projection(
        await conversation_detail(source["store"], source["conversation"])
    )
    assert projection["dataset_ref"] == child and not set(source["ids"]) & set(
        projection["analysis_ids"]
    )
    result = await report_attempt(source, tmp_path)
    assert result["contract"]["analysis_requirements"]["dataset_ref"] == child, result
    assert result["run"]["status"] != "completed", result
    assert not succeeded(result, "generate_report"), result


@pytest.mark.asyncio
async def test_unreadable_current_source_is_not_rescued_by_existing_artifacts(
    actual_sources, tmp_path
):
    source = actual_sources
    path = Path(get_settings().dataset_dir) / f"{source['ref']}.parquet"
    assert path.resolve().is_relative_to(tmp_path.resolve()) and path.is_file()
    (tmp_path / "owned-current-source-before-removal.parquet").write_bytes(path.read_bytes())
    path.unlink()  # Only this test's owned data file; no persisted success is edited.
    result = await report_attempt(source, tmp_path)
    projection = support.execution_projection(
        await conversation_detail(source["store"], source["conversation"])
    )
    support.write_witness(
        tmp_path, "unreadable-current-context", projection=projection, result=result
    )
    assert projection["availability"] != "available", projection
    assert result["run"]["status"] != "completed", result
    assert not succeeded(result, "generate_report"), result


@pytest.mark.asyncio
async def test_removed_actual_total_fragment_fails_full_completion(actual_sources, tmp_path):
    result = await report_attempt(actual_sources, tmp_path)
    assert result["run"]["status"] == "completed", result
    before, reports, contract, calls, evidence, sources = full_verdict(actual_sources, result)
    assert before.passed and len(reports) == 1, before
    assert contract.analysis_requirements.total
    report = reports[0]
    path = Path(report.file_ref).with_suffix(".md")
    assert path.resolve().is_relative_to(tmp_path.resolve())
    original = path.read_text()
    altered, count = re.subn(r"(?m)^完整数据总计：[^\n]*\n?", "", original)
    assert count == 1 and altered != original
    (tmp_path / "original-successful-report-before-total-removal.md").write_text(original)
    path.write_text(altered)
    after, *_ = full_verdict(actual_sources, result)
    call = next(x for x in calls if x.tool_name == "generate_report" and x.status == "succeeded")
    ev = next(x for x in evidence if x.invocation_id == call.invocation_id)
    support.write_witness(
        tmp_path,
        "removed-total-completion",
        before=asdict(before),
        after=asdict(after),
        original_hash=hashlib.sha256(original.encode()).hexdigest(),
        altered_hash=hashlib.sha256(altered.encode()).hexdigest(),
        unchanged_database_run=result["run"],
        contract=contract.to_dict(),
    )
    assert not verified_report_totals(report, call, ev, sources)
    assert not after.passed, after
    assert any(x.code == "missing_required_total_evidence" for x in after.issues), after
