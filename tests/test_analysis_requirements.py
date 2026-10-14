"""r5/r7 回归：筛选不可缩水、总计不可删除、表格确实进入 PDF。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from apps.orchestrator.agent_loop import (
    AgentLoopConfig,
    ConversationLockPool,
    _filter_contract_after_answer,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import AgentContext, _assemble_report_args
from apps.orchestrator.control.claims import build_evidence_summary, extract_claims
from apps.orchestrator.control.verifier import (
    _last_unrecovered_execution_failure,
    verify_completion,
)
from mcp_servers.report.server import build_server as build_report
from packages.common.dataset_store import save_dataframe
from packages.models.types import ModelResponse, ToolCall
from packages.orchestration.analysis_requirements import (
    AnalysisRequirements,
    explicit_analysis_requirements,
)
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_models import EvidenceRecord, ToolInvocation
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry, build_chart, build_excel

FILTERS = [
    {"column": "地区", "op": "==", "value": "华东"},
    {"column": "渠道", "op": "==", "value": "线上"},
]


def contract(text: str = "请给出维护成本总计。") -> Any:
    return build_minimal_contract(
        run_id="run",
        user_text=text,
        columns=("地区", "渠道", "月份", "维护成本"),
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )


@pytest.mark.parametrize(
    "text",
    [
        "过滤条件为 地区=华东 AND 渠道=线上。回答维护成本总计，不要心算。",
        "筛选：地区 = 华东 且 渠道 = 线上；给出维护成本总计。",
        '筛选条件为 地区="华东" AND 渠道="线上"。给出维护成本合计。',
    ],
)
def test_requirements_preserve_complete_conditions_and_roundtrip(text: str) -> None:
    required = contract(text).analysis_requirements
    assert list(required.filters) == FILTERS
    assert required.total and required.total_column == "维护成本"
    assert required.filter_error is None
    assert AnalysisRequirements.from_dict(required.to_dict()) == required


def test_user_correction_preserves_total_and_source() -> None:
    original = contract(
        "数据集 " + "a" * 32 + "。过滤条件为 地区=华东 AND 品类=硬件。维护成本总计。"
    )
    corrected = _filter_contract_after_answer(original, "过滤条件为 地区=华东 AND 渠道=线上")
    assert list(corrected.analysis_requirements.filters) == FILTERS
    assert corrected.analysis_requirements.total
    assert corrected.analysis_requirements.dataset_ref == "a" * 32
    assert original.analysis_requirements.filters[-1]["column"] == "品类"


@pytest.mark.parametrize(
    "text",
    [
        "过滤条件为 地区=华东 OR 渠道=线上",
        "过滤条件为 地区=华东 AND 设备 名称=转播车",
    ],
)
def test_unparsed_comparison_conditions_require_clarification(text: str) -> None:
    assert contract(text).analysis_requirements.filter_error


def test_negation_does_not_cancel_required_total_elsewhere() -> None:
    assert not explicit_analysis_requirements("无需总计。只需每月数据。").total
    assert explicit_analysis_requirements("不要心算。请给出维护成本总计。").total


def invocation(name: str = "dimension_contribution", **kwargs: Any) -> ToolInvocation:
    return replace(
        ToolInvocation(
            invocation_id="inv",
            run_id="run",
            step_id="step",
            tool_call_id="call",
            tool_name=name,
            idempotency_key="key",
            args_hash="args",
            args={"dataset_ref": "source", "value_col": "维护成本"},
            status="succeeded",
            result_hash="result",
            error_text=None,
            artifact_id=None,
            started_at="1",
            completed_at="2",
        ),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("answer", "metric", "issue"),
    [
        ("维护成本总计为 1170。", "维护成本", None),
        ("维护成本总计为 0.117 万。", "维护成本", None),
        ("已列出各月份值。", "维护成本", "missing_required_total_delivery"),
        ("删除没有证据的总计 1170。", "维护成本", "missing_required_total_delivery"),
        ("维护成本总计为 1170。", "设备数", "missing_required_total_evidence"),
    ],
)
def test_total_must_be_delivered_with_correct_metric_evidence(
    answer: str,
    metric: str,
    issue: str | None,
) -> None:
    tool = invocation(args={"dataset_ref": "source", "value_col": metric})
    evidence = EvidenceRecord(
        evidence_id="evidence",
        run_id="run",
        invocation_id=tool.invocation_id,
        artifact_id=None,
        kind="tool_result",
        source={"tool": tool.tool_name},
        result_hash="result",
        summary=build_evidence_summary(
            summary="维护成本汇总",
            result={"total_value": 1170},
            artifact_id=None,
        ),
        created_at="now",
    )
    task = contract()
    result = verify_completion(
        contract=task,
        final_text=answer,
        artifacts=[],
        invocations=[tool],
        evidence=[evidence],
        claims=extract_claims(final_text=answer, goal=task.goal, evidence=[evidence]),
    )
    if issue:
        assert issue in {item.code for item in result.issues}
    else:
        assert result.passed, result


def test_recovery_cannot_hide_an_earlier_failure_or_weaken_filters() -> None:
    failed = invocation(
        "transform_dataset",
        status="failed",
        error_text="字段不存在",
        args={"dataset_ref": "source", "filters": FILTERS},
    )
    weaker = replace(
        failed,
        invocation_id="retry",
        status="succeeded",
        error_text=None,
        args={"dataset_ref": "source", "filters": FILTERS[:1]},
    )
    assert _last_unrecovered_execution_failure([failed, weaker]) == failed
    assert (
        _last_unrecovered_execution_failure(
            [
                failed,
                replace(weaker, args=failed.args, run_id="other-run"),
            ]
        )
        == failed
    )
    unrelated_fail = invocation("forecast", status="failed", error_text="点数不足")
    recovered = replace(weaker, args=failed.args)
    assert (
        _last_unrecovered_execution_failure([unrelated_fail, failed, recovered]) == unrelated_fail
    )
    assert _last_unrecovered_execution_failure([failed, recovered]) is None


@pytest.fixture
def analysis_workspace(tmp_path: Path) -> tuple[SessionStore, AgentContext, str]:
    store = SessionStore(str(tmp_path / "analysis.db"))
    project = store.create_project("设备维护合成数据")
    conversation = store.create_conversation(project.id)
    # 额外的华东线下记录使漏掉渠道必然改变数值，消除 r5 的巧合 oracle。
    rows = [
        {"月份": f"2026-{month:02d}", "地区": region, "渠道": channel, "维护成本": amount}
        for month in range(1, 7)
        for region, channel, amount in [
            ("华东", "线上", 18 + month * 6),
            ("华南", "线下", 21),
            ("华东", "线下", 999),
        ]
        for _ in range(5)
    ]
    ref = save_dataframe(pd.DataFrame(rows))
    profile = build_excel()._tools["infer_schema"].invoke({"dataset_ref": ref}).to_dict()
    store.register_dataset(
        ref=ref, project_id=project.id, filename="维护记录.xlsx", profile=profile
    )
    return store, AgentContext(store, project.id, conversation.id), ref


class AnalysisGateway:
    """仅模型响应替身；工具、治理、持久化和 Evidence 都执行真实实现。"""

    def __init__(self, workspace: tuple[SessionStore, AgentContext, str], mode: str) -> None:
        self.store, self.context, self.ref = workspace
        self.mode = mode
        self.turns = 0

    async def stream_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[str | ModelResponse]:
        ordinal = self.turns
        self.turns += 1
        calls = []
        if ordinal == 0:
            calls = [
                (
                    "transform_dataset",
                    {
                        "dataset_ref": self.ref,
                        "filters": FILTERS[:1] if self.mode == "weakened" else FILTERS,
                    },
                )
            ]
        elif ordinal == 1 and self.mode != "weakened":
            derived = [
                item
                for item in self.store.list_datasets(self.context.project_id)
                if item.parent_ref == self.ref
            ]
            assert len(derived) == 1
            shared = {"dataset_ref": derived[0].ref, "group_col": "月份", "value_col": "维护成本"}
            calls = [
                ("aggregate_preview", {**shared, "agg": "sum"}),
            ]
        content = (
            ""
            if calls
            else ("已列出各月份值。" if self.mode == "omitted" else "维护成本总计为1170。")
        )
        yield ModelResponse(
            content=content,
            model="offline-scripted",
            tool_calls=[
                ToolCall(id=f"call-{ordinal}-{index}", name=name, arguments=json.dumps(params))
                for index, (name, params) in enumerate(calls)
            ],
        )


async def run_analysis(
    workspace: tuple[SessionStore, AgentContext, str],
    mode: str,
    text: str | None = None,
    **kwargs: Any,
) -> tuple[Any, Any]:
    store, context, ref = workspace
    gateway = AnalysisGateway(workspace, mode)
    registry = _registry(context=context)
    try:
        raw = [
            item
            async for item in stream_agent_chat(
                conversation_id=context.conversation_id,
                project_id=context.project_id,
                store=store,
                user_text=text
                or (
                    f"数据集 {ref}。过滤条件为 地区=华东 AND 渠道=线上。"
                    "以月份分组汇总维护成本，并回答维护成本总计。"
                ),
                gateway=gateway,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=6),
                **kwargs,
            )
        ]
    finally:
        await registry.aclose()
    return [(item["event"], json.loads(item["data"])) for item in raw], gateway


@pytest.mark.parametrize("mode", ["valid", "weakened", "omitted"])
async def test_real_tools_persist_requirements_filters_scope_and_total(
    analysis_workspace: tuple[SessionStore, AgentContext, str],
    mode: str,
) -> None:
    events, gateway = await run_analysis(analysis_workspace, mode)
    done = dict(events).get("done") or dict(events)["error"]
    assert done["run_status"] == ("completed" if mode == "valid" else "blocked"), events
    store, context, ref = analysis_workspace
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(done["run_id"])
    assert run is not None and run.status == done["run_status"]
    assert tasks.get_contract(run.run_id)["analysis_requirements"]["filters"] == FILTERS
    derived = [item for item in store.list_datasets(context.project_id) if item.parent_ref == ref]
    invocations = tasks.list_invocations(run.run_id)
    if mode == "weakened":
        assert not derived
        assert not any(item.status == "succeeded" for item in invocations)
    else:
        assert len(derived) == 1 and derived[0].transform["filters"] == FILTERS
        assert derived[0].profile["row_count"] == 30
        assert all(
            item.args["dataset_ref"] == derived[0].ref
            for item in invocations
            if item.tool_name in {"aggregate_preview", "dimension_contribution"}
        )
        evidence = tasks.list_evidence(run.run_id)
        assert len(evidence) == 2
        assert any(
            value["path"] == "$.total_value" and float(value["value"]) == 1170
            for item in evidence
            for value in item.summary["value_index"]
        )
        if mode == "omitted":
            assert run.terminal_reason == "missing_required_total_delivery"
    assert gateway.turns <= 4


async def test_missing_filter_waits_without_model_then_resumes_with_user_correction(
    analysis_workspace: tuple[SessionStore, AgentContext, str],
) -> None:
    store, _, ref = analysis_workspace
    original = (
        f"数据集 {ref}。过滤条件为 地区=华东 AND 品类=硬件。"
        "以月份分组汇总维护成本，并回答维护成本总计。"
    )
    events, gateway = await run_analysis(analysis_workspace, "valid", original)
    assert gateway.turns == 0
    done = dict(events).get("done") or dict(events)["error"]
    assert done["run_status"] == "waiting_user"
    waiting = dict(events)["waiting_user"]["payload"]
    assert waiting["about"] == "filter_columns"
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(done["run_id"])
    answer = "过滤条件为 地区=华东 AND 渠道=线上"
    tasks.answer_clarification(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="correct-filter",
        question_id="filter_columns",
        resume_token=waiting["resume_token"],
        answer=answer,
    )
    resumed, _ = await run_analysis(
        analysis_workspace,
        "valid",
        original,
        run_id=run.run_id,
        resume_existing=True,
        clarification_question_id="filter_columns",
        clarification_answer=answer,
    )
    assert dict(resumed)["done"]["run_status"] == "completed", resumed
    assert tasks.get_contract(run.run_id)["analysis_requirements"]["filters"] == FILTERS


def test_selected_table_rows_survive_real_markdown_and_pdf_render(
    analysis_workspace: tuple[SessionStore, AgentContext, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import weasyprint

    store, context, ref = analysis_workspace
    registry = _registry(context=context)
    result = registry.execute(
        "aggregate_preview",
        json.dumps(
            {
                "dataset_ref": ref,
                "group_col": "月份",
                "value_col": "维护成本",
                "agg": "sum",
            }
        ),
    )
    table = Artifact(
        id="table",
        conversation_id=context.conversation_id,
        message_id="message",
        type="table",
        payload=result,
        file_ref=None,
        source_tool="aggregate_preview",
        params={"analysis_id": "monthly-analysis"},
        dataset_ref=ref,
        created_at="now",
    )
    args, skipped = _assemble_report_args(build_chart(), [table])
    assert skipped == 0 and args["tables"][0]["analysis_id"] == "monthly-analysis"
    report = build_report()
    md = report._tools["gen_report_md"].invoke({"title": "设备维护成本", **args})
    rendered = []
    real_html = weasyprint.HTML

    class CapturedHTML:
        def __init__(self, **kwargs: Any) -> None:
            self.html = real_html(**kwargs)

        def write_pdf(self) -> bytes:
            document = self.html.render()
            for page in document.pages:
                rendered.extend(
                    box.text for box in page._page_box.descendants() if hasattr(box, "text")
                )
            return document.write_pdf()

    monkeypatch.setattr(weasyprint, "HTML", CapturedHTML)
    pdf = report._tools["export_pdf"].invoke({"report_id": md["report_id"]})
    assert Path(pdf["pdf_path"]).read_bytes().startswith(b"%PDF-")
    for row in result["rows"]:
        assert str(row["group"]) in md["markdown"] and str(row["group"]) in rendered
        assert str(row["value"]) in md["markdown"] and str(row["value"]) in rendered
    assert "monthly-analysis" in " ".join(rendered)
