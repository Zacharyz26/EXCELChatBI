"""Frozen independent R4 default-consumer tests; NOT_RUN until root execution.

Real upload API, Host, tools, Evidence, complete Verifier and SQLite. Only model
choices are scripted. No hand-written success records or product-state edits.
No PDF/browser claim: these tests exercise actual chart data and Markdown report
selection; the separate representative chain owns PDF and visual acceptance.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_local_chain_draft import api_request, artifact_id, conversation_detail

METRIC = support.METRIC


def oracle(*, filtered=False, nonnull=False):
    rows = list(csv.DictReader(io.StringIO(support.CSV.decode())))
    if filtered:
        rows = [r for r in rows if r["频道"] == "A" and r["状态"] == "已播出"]
    if nonnull:
        rows = [r for r in rows if r[METRIC].strip()]
    monthly = {}
    for row in rows:
        monthly.setdefault(row["月份"], Decimal(0))
        if row[METRIC].strip():
            monthly[row["月份"]] += Decimal(row[METRIC])
    return rows, dict(sorted(monthly.items())), sum(monthly.values(), Decimal(0))


async def upload(store, project, conversation, *, body=support.CSV, name="source.csv"):
    # Actual handler, no app lifespan/recover/shutdown while other Host runs exist.
    response = await api_request(
        store,
        "POST",
        "/upload/excel",
        data={"project_id": project.id, "conversation_id": conversation.id},
        files={"file": (name, body, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()["dataset_ref"]


def successful(result, tool):
    return [
        r for r in result["invocations"] if r["tool_name"] == tool and r["status"] == "succeeded"
    ]


async def aggregate(store, project, conversation, ref, total, tmp_path, label):
    before = {a.id for a in store.list_artifacts(conversation.id)}
    result = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                (
                    "aggregate_preview",
                    {
                        "dataset_ref": ref,
                        "group_col": "月份",
                        "value_col": METRIC,
                        "agg": "sum",
                    },
                ),
            ],
            f"{METRIC}总计为{total}。",
        ),
        f"dataset_ref={ref}；按月份分组汇总{METRIC}并给出总计。",
    )
    artifacts = [
        a
        for a in store.list_artifacts(conversation.id)
        if a.id not in before and a.source_tool == "aggregate_preview"
    ]
    support.write_witness(tmp_path, label, result=result, artifacts=[asdict(a) for a in artifacts])
    assert result["run"]["status"] == "completed", result
    assert len(successful(result, "aggregate_preview")) == len(artifacts) == 1, result
    assert Decimal(str(artifacts[0].payload["total_value"])) == Decimal(total)
    return artifacts[0], result


async def source_with_old_analysis(store, project, conversation, tmp_path):
    original = await upload(store, project, conversation)
    old, old_result = await aggregate(
        store,
        project,
        conversation,
        original,
        oracle()[2],
        tmp_path,
        "old-source-aggregate",
    )
    result = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                ("transform_dataset", {"dataset_ref": original, "filters": support.FILTERS}),
            ],
            "筛选前6行，筛选后4行。",
        ),
        f"dataset_ref={original}；筛选条件：频道=A，状态=已播出。说明筛选前后的行数。",
    )
    support.write_witness(tmp_path, "filter-setup", original=original, result=result)
    assert result["run"]["status"] == "completed", result
    records = [e for e in result["evidence"] if e["source"].get("tool") == "transform_dataset"]
    assert len(successful(result, "transform_dataset")) == len(records) == 1
    child = records[0]["summary"]["fact_projection"]["output_ref"]
    assert child != original and store.get_dataset(child).parent_ref == original
    return original, child, old, old_result


async def default_chart(store, project, conversation):
    # dataset_ref intentionally absent: this is the production Host default path.
    return await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                (
                    "gen_chart",
                    {
                        "chart_type": "line",
                        "encoding": {
                            "x": "月份",
                            "y": METRIC,
                            "agg": "sum",
                        },
                    },
                ),
            ],
            "已生成当前数据的月度时长折线图。",
        ),
        f"请为当前数据生成按月份展示{METRIC}的折线图。",
    )


def assert_child_chart(store, conversation, result, child):
    assert result["run"]["status"] == "completed", result
    calls = successful(result, "gen_chart")
    assert len(calls) == 1 and calls[0]["args"]["dataset_ref"] == child, result
    artifact = next(
        a for a in store.list_artifacts(conversation.id) if a.id == calls[0]["artifact_id"]
    )
    assert artifact.dataset_ref == child
    option = artifact.payload["option"]
    # R4-CHART-POLICY-01: unchanged four-row fixture has only 2+1 valid
    # measurements. Existing min-size 5 policy suppresses even the merged
    # bucket; raw CSV sums are not the permitted presentation oracle.
    from packages.governance.data_boundary import resolve_policy

    policy = resolve_policy(child)
    assert policy.small_group_min_size == 5 and policy.small_group_mode == "merge"
    raw_rows = oracle(filtered=True)[0]
    assert sum(bool(row[METRIC].strip()) for row in raw_rows) == 3
    assert option["xAxis"]["type"] == "category" and option["yAxis"]["type"] == "value"
    assert option["xAxis"]["data"] == []
    assert option["series"][0]["data"] == []
    assert METRIC in str(option["series"][0].get("name", ""))


async def report(store, project, conversation, *, goal="请把当前数据已有的分析生成报告。"):
    # analysis_ids absent: no helper fills it from a fixture or expected output.
    return await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                ("generate_report", {"title": "当前分析报告", "include_pdf": False}),
            ],
            "指定分析报告已生成。",
        ),
        goal,
    )


def assert_report(store, conversation, result, selected_id, total, tmp_path):
    assert result["run"]["status"] == "completed", result
    calls = successful(result, "generate_report")
    assert len(calls) == 1 and calls[0]["args"]["analysis_ids"] == [selected_id], result
    artifact = next(
        a for a in store.list_artifacts(conversation.id) if a.id == calls[0]["artifact_id"]
    )
    payload = artifact.payload
    assert artifact.params["analysis_ids"] == [selected_id]
    content = Path(artifact.file_ref).read_bytes()
    text = content.decode("utf-8")
    assert f"完整数据总计：{total}" in text and selected_id in text and METRIC in text
    assert hashlib.sha256(content).hexdigest() == payload["validation"]["content_sha256"]
    assert any(
        e["source"].get("tool") == "generate_report" and e["summary"].get("report_validation_hash")
        for e in result["evidence"]
    )
    (tmp_path / "generated-report.md").write_bytes(content)


@pytest.mark.asyncio
async def test_default_chart_uses_current_child_not_project_latest(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    original, child, old, _ = await source_with_old_analysis(store, project, conversation, tmp_path)
    unrelated = store.create_conversation(project.id)
    foreign_latest = await upload(
        store, project, unrelated, body=support.OTHER, name="project-latest.csv"
    )
    result = await default_chart(store, project, conversation)
    support.write_witness(
        tmp_path,
        "default-chart",
        result=result,
        original=original,
        child=child,
        project_latest=foreign_latest,
        old_analysis=asdict(old),
    )
    assert len({original, child, foreign_latest}) == 3
    assert_child_chart(store, conversation, result, child)


@pytest.mark.asyncio
async def test_default_report_selects_current_analysis_not_old_source(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    original, child, old, _ = await source_with_old_analysis(store, project, conversation, tmp_path)
    current, _ = await aggregate(
        store,
        project,
        conversation,
        child,
        oracle(filtered=True)[2],
        tmp_path,
        "current-source-aggregate",
    )
    result = await report(store, project, conversation)
    support.write_witness(
        tmp_path,
        "default-report",
        result=result,
        original=original,
        child=child,
        old_analysis=asdict(old),
        current_analysis=asdict(current),
    )
    assert artifact_id(old) != artifact_id(current)
    assert_report(
        store, conversation, result, artifact_id(current), oracle(filtered=True)[2], tmp_path
    )


@pytest.mark.asyncio
async def test_explicit_old_analysis_report_does_not_change_future_default_dataset(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    original, child, old, _ = await source_with_old_analysis(store, project, conversation, tmp_path)
    result = await report(
        store, project, conversation, goal=f"请使用 analysis_id={artifact_id(old)} 生成报告。"
    )
    detail = await conversation_detail(store, conversation)
    support.write_witness(
        tmp_path,
        "explicit-old-report",
        result=result,
        detail=detail,
        original=original,
        child=child,
        old_analysis=asdict(old),
    )
    assert_report(store, conversation, result, artifact_id(old), oracle()[2], tmp_path)
    projection = support.execution_projection(detail)
    assert (
        projection["availability"] == "available" and projection["dataset_ref"] == child
    ), projection
    chart = await default_chart(store, project, conversation)
    support.write_witness(tmp_path, "chart-after-explicit-old-report", result=chart, child=child)
    assert_child_chart(store, conversation, chart, child)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_child_analysis", [False, True], ids=["no-current-analysis", "older-child-version"]
)
async def test_default_report_never_falls_back_when_current_analysis_missing(
    tmp_path, stale_child_analysis
):
    store, project, conversation = support.workspace(tmp_path)
    original, child, old, _ = await source_with_old_analysis(store, project, conversation, tmp_path)
    stale = None
    current = child
    if stale_child_analysis:
        stale, _ = await aggregate(
            store,
            project,
            conversation,
            child,
            oracle(filtered=True)[2],
            tmp_path,
            "older-child-aggregate",
        )
        cleaning = await support.execute(
            store,
            project,
            conversation,
            support.ScriptedModel(
                [
                    ("transform_dataset", {"dataset_ref": child, "drop_nulls": [METRIC]}),
                ],
                "清洗前4行，清洗后3行。",
            ),
            f"dataset_ref={child}；删除{METRIC}为空的行，说明清洗前后的行数。",
        )
        support.write_witness(tmp_path, "newer-child-cleaning", result=cleaning)
        assert cleaning["run"]["status"] == "completed", cleaning
        records = [
            e for e in cleaning["evidence"] if e["source"].get("tool") == "transform_dataset"
        ]
        assert len(records) == 1
        current = records[0]["summary"]["fact_projection"]["output_ref"]
        assert current != child and store.get_dataset(current).parent_ref == child
        # Same sum deliberately cannot prove the same version or same population.
        assert oracle(filtered=True, nonnull=True)[2] == oracle(filtered=True)[2]
    result = await report(store, project, conversation)
    detail = await conversation_detail(store, conversation)
    support.write_witness(
        tmp_path,
        "missing-current-report",
        result=result,
        detail=detail,
        original=original,
        current=current,
        old_analysis=asdict(old),
        stale_analysis=asdict(stale) if stale else None,
    )
    assert result["run"]["status"] != "completed", result
    assert not successful(result, "generate_report"), result
    assert not [
        a for a in store.list_artifacts(conversation.id) if a.source_tool == "generate_report"
    ]
    projection = support.execution_projection(detail)
    assert projection["availability"] == "available" and projection["dataset_ref"] == current
    assert artifact_id(old) not in projection.get("analysis_ids", [])
    if stale is not None:
        assert artifact_id(stale) not in projection.get("analysis_ids", [])
