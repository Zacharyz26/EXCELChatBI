"""第二批实际失败的正反例：表格、报告交付、衍生图表与轴角色。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from apps.orchestrator.agent_loop import _analysis_scope_error, _enrich_tool_arguments
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.artifacts import artifact_payload_for
from apps.orchestrator.control.claims import extract_claims
from apps.orchestrator.control.verifier import verify_completion
from apps.orchestrator.request_intent import required_chart_dimensions
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.dataset_scope import verified_analysis_scope
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_models import TaskDatasetBinding

from tests.test_agent_tools import _registry
from tests.test_batch1_contracts import D, F, evidence, invocation


@pytest.mark.parametrize(
    "row,accepted",
    [
        ("| 2026-01 | 120 |", True),
        ("| 2026-01 | 150 |", False),
        ("| 2026-07 | 120 |", False),
        ("| 2026-01 | 120万元 |", False),
    ],
)
def test_month_table_keeps_group_metric_and_unit_identity(row: str, accepted: bool) -> None:
    args = {"dataset_ref": D, "group_col": "月份", "value_col": "销售额", "agg": "sum"}
    items = [
        evidence(
            {
                "profile": {
                    "columns": [
                        {"name": "月份", "distinct_count": 6, "null_ratio": 0.0},
                        {"name": "销售额", "distinct_count": 6, "mean": 39.0},
                    ]
                }
            },
            {"dataset_ref": D},
            "profile",
        ),
        evidence(
            {"rows": [{"group": "2026-01", "value": 120}, {"group": "2026-02", "value": 150}]}, args
        ),
    ]
    answer = "| 月份 | 销售额 |\n|---|---|\n" + row
    claims = extract_claims(final_text=answer, goal="", evidence=items)
    assert claims
    assert all(ref["supported"] for c in claims for ref in c.value_refs) is accepted
    for bad in [
        answer.replace("销售额", "库存"),
        answer.replace("| 月份 |", "| 数据集 | 月份 |")
        .replace("|---|---|", "|---|---|---|")
        .replace(row, row.replace("| ", f"| {F} | ", 1)),
    ]:
        claims = extract_claims(final_text=bad, goal="", evidence=items)
        assert not all(ref["supported"] for c in claims for ref in c.value_refs)


def device_store(tmp_path: Path) -> tuple[SessionStore, str, str]:
    store = SessionStore(str(tmp_path / "state.db"))
    project = store.create_project("合成设备")
    conversation = store.create_conversation(project.id)
    profile = {
        "row_count": 100,
        "column_count": 4,
        "columns": [
            {"name": name, "dtype": dtype, "null_ratio": 0, "distinct_count": count}
            for name, dtype, count in [
                ("设备", "str", 4),
                ("状态", "str", 2),
                ("月份", "str", 6),
                ("停机分钟", "float", 70),
            ]
        ],
    }
    for ref in (D, F):
        store.register_dataset(
            ref=ref, project_id=project.id, filename=ref + ".xlsx", profile=profile
        )
    return store, project.id, conversation.id


@pytest.mark.parametrize(
    "text,expected",
    [
        ("筛选设备=A且状态=运行，按月份汇总停机分钟，生成折线图。", ("月份",)),
        ("筛选设备=A且状态=运行，生成折线图。", ()),
        ("筛选设备=A且状态=运行，生成图表，encoding.x=月份。", ("月份",)),
        ("按设备、状态分别生成柱状图。", ("设备", "状态")),
        ("生成设备和状态柱状图。", ("设备", "状态")),
        ("展示各维度列的数值列图表。", ("设备", "状态")),
    ],
)
def test_filter_fields_are_not_chart_axes(
    tmp_path: Path, text: str, expected: tuple[str, ...]
) -> None:
    store, project, _ = device_store(tmp_path)
    assert required_chart_dimensions(text, store.list_datasets(project)[:1]) == expected


@pytest.mark.parametrize(
    "mutation",
    ["valid", "unrelated", "source", "stale", "bad_hash", "wrong_parent", "missing_filter"],
)
def test_chart_enrichment_keeps_only_verified_derived_scope(tmp_path: Path, mutation: str) -> None:
    store, project, _ = device_store(tmp_path)
    filters = [{"column": "设备", "op": "==", "value": "A"}]
    args = {"dataset_ref": D, "filters": filters, "drop_nulls": ["停机分钟"]}
    tools = [invocation("transform_dataset", args, "clean")]
    records = [evidence({"dataset_ref": F}, args, "clean")]
    bindings = [TaskDatasetBinding("r", F, "derived", D, "clean", "now", "now")]
    ref = F
    if mutation == "unrelated":
        ref = "e" * 32
    if mutation == "source":
        ref = D
    if mutation == "stale":
        tools[0] = replace(tools[0], run_id="old")
    if mutation == "bad_hash":
        records[0] = replace(records[0], result_hash="wrong")
    if mutation == "wrong_parent":
        bindings[0] = replace(bindings[0], parent_ref="e" * 32)
    if mutation == "missing_filter":
        tools[0] = replace(tools[0], args={"dataset_ref": D})
    contract = build_minimal_contract(
        run_id="r",
        user_text="筛选设备=A，清洗后生成图表。",
        chart_required=True,
        report_required=False,
        pdf_required=False,
        dataset_refs=(D,),
    )
    scope = verified_analysis_scope(contract.analysis_requirements, "r", tools, records, bindings)
    facade = SimpleNamespace(
        list_invocations=lambda _: tools,
        list_evidence=lambda _: records,
        list_dataset_bindings=lambda _: bindings,
    )
    enriched = _enrich_tool_arguments(
        "gen_chart",
        {"dataset_ref": ref, "chart_type": "line", "encoding": {"x": "月份", "y": "停机分钟"}},
        contract=contract,
        datasets=store.list_datasets(project),
        artifacts=[],
        verified_chart_dataset_refs=frozenset(scope),
    )
    assert (_analysis_scope_error(facade, "r", contract, "gen_chart", enriched) is None) is (
        mutation == "valid"
    )


@pytest.mark.parametrize(
    "mutation,answer,accepted",
    [
        ("none", "报告已生成。", True),
        ("none", "销售额总计270。报告已生成。", True),
        ("none", "销售额总计999。报告已生成。", False),
        ("chat_required", "报告已生成。", False),
        ("chat_required", "销售额总计270。报告已生成。", True),
        ("source_value", "报告已生成。", False),
        ("source_unit", "报告已生成。", False),
        ("wrong_metric", "报告已生成。", False),
        ("wrong_dataset", "报告已生成。", False),
        ("wrong_conversation", "报告已生成。", False),
        ("stale_evidence", "报告已生成。", False),
        ("wrong_hash", "报告已生成。", False),
        ("failed_invocation", "报告已生成。", False),
        ("file_changed", "报告已生成。", False),
        ("validation_changed", "报告已生成。", False),
        ("source_not_selected", "报告已生成。", False),
        ("payload_metric", "报告已生成。", False),
        ("payload_aggregation", "报告已生成。", False),
        ("payload_unit", "报告已生成。", False),
        ("params_dataset", "报告已生成。", False),
    ],
)
def test_report_total_requires_source_evidence_and_actual_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, answer: str, accepted: bool
) -> None:
    from packages.common.config import get_settings

    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    store, project, conversation = device_store(tmp_path)
    message = store.append_message(conversation_id=conversation, role="assistant", content="分析")
    source = store.create_artifact(
        conversation_id=conversation,
        message_id=message.id,
        type="table",
        source_tool="aggregate_preview",
        dataset_ref=D,
        params={
            "analysis_id": "monthly",
            "dataset_ref": F if mutation == "params_dataset" else D,
            "group_col": "月份",
            "value_col": "销售额",
            "agg": "sum",
        },
        payload={
            "group_col": "月份",
            "value_col": "利润" if mutation == "payload_metric" else "销售额",
            "agg": "mean" if mutation == "payload_aggregation" else "sum",
            **({"unit": "万元"} if mutation == "payload_unit" else {}),
            "total_value": 270,
            "group_total": 2,
            "truncated": False,
            "rows": [
                {"group": "2026-01", "value": 120, "row_count": 5, "valid_value_count": 5},
                {"group": "2026-02", "value": 150, "row_count": 5, "valid_value_count": 5},
            ],
        },
    )
    args = {"analysis_ids": ["monthly"], "title": "报告", "include_pdf": False}
    result = _registry(AgentContext(store, project, conversation)).execute(
        "generate_report", json.dumps(args)
    )
    report = Artifact(
        "report",
        conversation,
        message.id,
        "report",
        artifact_payload_for("generate_report", result),
        result["md_path"],
        "generate_report",
        args,
        D,
        "now",
    )
    tool = replace(invocation("generate_report", args), artifact_id=report.id)
    record = replace(
        evidence(result, args), source={"tool": "generate_report"}, artifact_id=report.id
    )
    sources = [source]
    metric, ref = "销售额", D
    goal = "生成报告，正文包括销售额总计。"
    if mutation == "chat_required":
        goal += "同时回答销售额总计。"
    if mutation == "source_value":
        source.payload["total_value"] = 999
    if mutation == "source_unit":
        source.params["unit"] = "万元"
    if mutation == "wrong_metric":
        metric = "利润"
        goal = "生成报告，正文包括利润总计。"
    if mutation == "wrong_dataset":
        ref = F
    if mutation == "wrong_conversation":
        sources = [replace(source, conversation_id="other")]
    if mutation == "stale_evidence":
        record = replace(record, run_id="old")
    if mutation == "wrong_hash":
        record = replace(record, result_hash="other")
    if mutation == "failed_invocation":
        tool = replace(tool, status="failed", error_text="失败")
    if mutation == "file_changed":
        Path(result["md_path"]).write_text("报告已生成，但未交付任何数值。")
    if mutation == "validation_changed":
        report.payload["validation"] = {"status": "passed"}
    if mutation == "source_not_selected":
        tool = replace(tool, args={**args, "analysis_ids": ["other"]})
    contract = build_minimal_contract(
        run_id="r",
        user_text=goal,
        columns=(metric,),
        dataset_refs=(ref,),
        chart_required=False,
        report_required=True,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=answer,
        artifacts=[report],
        invocations=[tool],
        evidence=[record],
        claims=extract_claims(final_text=answer, goal=goal, evidence=[record]),
        source_artifacts=sources,
    )
    assert verdict.passed is accepted, verdict
    get_settings.cache_clear()
