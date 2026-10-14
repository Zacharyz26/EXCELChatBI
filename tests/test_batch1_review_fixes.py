"""独立复核三项问题的成对反例、真实清洗链与精确报告出口。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from apps.orchestrator.agent_loop import (
    AgentLoopConfig,
    ConversationLockPool,
    _analysis_scope_error,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import AgentContext, _assemble_report_args
from apps.orchestrator.control.claims import extract_claims
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.dataset_ops.tools import aggregate_preview
from mcp_servers.report.server import build_server
from packages.common.dataset_store import save_dataframe
from packages.models.types import ModelResponse, ToolCall
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_models import TaskDatasetBinding
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry, build_excel
from tests.test_batch1_contracts import D, F, evidence, invocation


@pytest.mark.parametrize("kind", ["table", "bar", "pie"])
@pytest.mark.parametrize(
    "answer, accepted",
    [
        ("900元对应East销售额；100元对应West销售额。", False),
        ("100元对应East销售额；900元对应West销售额。", True),
        ("销售额，East数值为900元。", False),
        ("销售额，East数值为100元。", True),
        ("East利润为100元。", False),
        ("North销售额为100元。", False),
        ("East和West销售额为100元。", False),
        ("销售额为100元。", False),
        ("| 地区 | 销售额 |\n|---|---|\n| East | 900 |\n| West | 100 |", False),
        ("| 地区 | 销售额 |\n|---|---|\n| East | 100 |\n| West | 900 |", True),
        ("| 销售额 | 地区 |\n|---|---|\n| 100 | East |\n| 900 | West |", True),
        ("| 地区 | 利润 |\n|---|---|\n| East | 100 |\n| West | 900 |", False),
    ],
)
def test_structured_relations_agree_in_final_and_report(
    kind: str,
    answer: str,
    accepted: bool,
) -> None:
    args = {"dataset_ref": D, "group_col": "地区", "value_col": "销售额", "agg": "sum"}
    data: dict[str, Any] = {
        "rows": [{"group": "East", "value": 100}, {"group": "West", "value": 900}]
    }
    source = "aggregate_preview"
    if kind != "table":
        source = "gen_chart"
        args = {
            "dataset_ref": D,
            "encoding": {"x": "地区", "y": "销售额", "agg": "sum"},
        }
        points: list[Any] = (
            [100, 900]
            if kind == "bar"
            else [{"name": "East", "value": 100}, {"name": "West", "value": 900}]
        )
        data = {
            "option": {
                "xAxis": {"type": "category", "data": ["East", "West"]},
                "series": [{"type": kind, "name": "销售额", "data": points}],
            }
        }
    item = evidence(data, args)
    claims = extract_claims(final_text=answer, goal="说明销售额", evidence=[item])
    assert bool(claims)
    assert (
        all(ref["supported"] for claim in claims for ref in claim.value_refs)
        is accepted
    )
    artifact = Artifact(
        "a",
        "c",
        "m",
        "table" if kind == "table" else "chart",
        data,
        None,
        source,
        args,
        D,
        "now",
    )
    if accepted:
        result = validate_report_narratives(
            [artifact], {"title": "报告", "insights": answer}
        )
        assert result["status"] == "passed"
    else:
        with pytest.raises(ValueError, match="数值缺少"):
            validate_report_narratives(
                [artifact], {"title": "报告", "insights": answer}
            )


@pytest.mark.parametrize(
    "answer, accepted",
    [
        ("10元是销售额。", False),
        ("900元是销售额。", True),
        ("销售额，数值为10元。", False),
        ("销售额，数值为900元。", True),
        ("利润为900元。", False),
        ("记录数为10。", True),
    ],
)
def test_measurement_never_borrows_count(answer: str, accepted: bool) -> None:
    item = evidence({"row_count": 10, "total_value": 900}, {"value_col": "销售额"})
    claims = extract_claims(final_text=answer, goal="", evidence=[item])
    assert (
        all(ref["supported"] for claim in claims for ref in claim.value_refs)
        is accepted
    )


@pytest.mark.parametrize(
    "different",
    [
        {"dataset_ref": "e" * 32},
        {"filters": [{"column": "地区", "op": "==", "value": "West"}]},
        {"agg": "mean"},
        {"data_version_hash": "stale"},
    ],
)
def test_same_value_with_different_semantics_requires_clarification(
    different: dict[str, Any],
) -> None:
    args = {"dataset_ref": D, "value_col": "销售额", "agg": "sum"}
    items = [
        evidence({"total_value": 900}, args),
        evidence({"total_value": 900}, {**args, **different}, "other"),
    ]
    claims = extract_claims(final_text="销售额为900元。", goal="", evidence=items)
    assert claims[0].value_refs[0]["supported"] is False


def test_sum_cannot_support_mean() -> None:
    item = evidence({"total_value": 900}, {"value_col": "销售额", "agg": "sum"})
    claims = extract_claims(
        final_text="销售额平均值为900元。", goal="", evidence=[item]
    )
    assert claims[0].value_refs[0]["supported"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "chain",
        "unrelated",
        "stale",
        "failed",
        "no_evidence",
        "bad_hash",
        "wrong_parent",
        "wrong_run",
        "wrong_producer",
        "unregistered",
        "cycle",
        "filters",
    ],
)
def test_lineage_scope_requires_success_evidence_and_exact_parent(
    mutation: str,
) -> None:
    clean_args = {"dataset_ref": D, "drop_nulls": ["销售额"]}
    clean = invocation("transform_dataset", clean_args, "clean")
    items = [evidence({"dataset_ref": F}, clean_args, "clean")]
    tools = [clean]
    bindings = [TaskDatasetBinding("r", F, "derived", D, "clean", "now", "now")]
    ref = F
    if mutation == "chain":
        next_args = {"dataset_ref": F, "drop_duplicates": True}
        tools.append(invocation("transform_dataset", next_args, "dedup"))
        items.append(evidence({"dataset_ref": "g" * 32}, next_args, "dedup"))
        bindings.append(
            TaskDatasetBinding("r", "g" * 32, "derived", F, "dedup", "now", "now")
        )
        ref = "g" * 32
    elif mutation == "unrelated":
        ref = "e" * 32
    elif mutation == "stale":
        tools[0] = replace(clean, run_id="old")
        items[0] = replace(items[0], run_id="old")
        bindings[0] = replace(bindings[0], run_id="old")
    elif mutation == "failed":
        tools[0] = replace(clean, status="failed")
    elif mutation == "no_evidence":
        items = []
    elif mutation == "bad_hash":
        items[0] = replace(items[0], result_hash="different")
    elif mutation == "wrong_parent":
        bindings[0] = replace(bindings[0], parent_ref="e" * 32)
    elif mutation == "wrong_run":
        bindings[0] = replace(bindings[0], run_id="other")
    elif mutation == "wrong_producer":
        tools[0] = replace(clean, tool_name="aggregate_preview")
    elif mutation == "unregistered":
        bindings = []
    elif mutation == "cycle":
        tools[0] = replace(clean, args={"dataset_ref": F, "drop_duplicates": True})
        bindings[0] = replace(bindings[0], parent_ref=F)
    goal = "删除销售额缺失行后分析销售额趋势。"
    if mutation == "filters":
        goal = "筛选条件为 地区=East。" + goal
    contract = build_minimal_contract(
        run_id="r",
        user_text=goal,
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(D,),
    )
    store = SimpleNamespace(
        list_invocations=lambda _: tools,
        list_evidence=lambda _: items,
        list_dataset_bindings=lambda _: bindings,
    )
    args = {"dataset_ref": ref, "time_col": "日期", "value_col": "销售额"}
    accepted = mutation in {"valid", "chain"}
    assert (
        _analysis_scope_error(store, "r", contract, "trend_analysis", args) is None
    ) is accepted
    result = verify_completion(
        contract=contract,
        final_text="分析完成。",
        artifacts=[],
        invocations=[*tools, invocation("trend_analysis", args)],
        evidence=[*items, evidence({"slope": 2}, args)],
        claims=[],
        dataset_bindings=bindings,
    )
    assert result.passed is accepted


async def test_real_cleaning_then_trend_completes_host_sqlite(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "clean.db"))
    project = store.create_project("合成清洗")
    conversation = store.create_conversation(project.id)
    context = AgentContext(store, project.id, conversation.id)
    frame = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=12, freq="MS"),
            "销售额": [float(i * 10) if i != 3 else None for i in range(12)],
        }
    )
    ref = save_dataframe(frame)
    profile = (
        build_excel()._tools["infer_schema"].invoke({"dataset_ref": ref}).to_dict()
    )
    store.register_dataset(
        ref=ref,
        project_id=project.id,
        filename="synthetic.csv",
        profile=profile,
    )

    class Gateway:
        turns = 0

        async def stream_turn(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[ModelResponse]:
            self.turns += 1
            calls = []
            if self.turns == 1:
                calls = [
                    ToolCall(
                        id="clean",
                        name="transform_dataset",
                        arguments=json.dumps(
                            {"dataset_ref": ref, "drop_nulls": ["销售额"]}
                        ),
                    )
                ]
            elif self.turns == 2:
                derived = next(
                    d for d in store.list_datasets(project.id) if d.parent_ref == ref
                )
                calls = [
                    ToolCall(
                        id="trend",
                        name="trend_analysis",
                        arguments=json.dumps(
                            {
                                "dataset_ref": derived.ref,
                                "time_col": "日期",
                                "value_col": "销售额",
                            }
                        ),
                    )
                ]
            yield ModelResponse(
                content="" if calls else "清洗后趋势分析完成，请查看统计工件。",
                model="offline-scripted",
                tool_calls=calls,
            )

    registry = _registry(context)
    try:
        events = [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=f"数据集 {ref}。删除销售额缺失行后分析销售额趋势。",
                gateway=Gateway(),
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=4),
            )
        ]
    finally:
        await registry.aclose()
    decoded = {item["event"]: json.loads(item["data"]) for item in events}
    done = decoded.get("done") or decoded["error"]
    assert done["run_status"] == "completed", decoded
    tasks = TaskStore(store.db_path)
    calls = tasks.list_invocations(done["run_id"])
    assert [item.tool_name for item in calls if item.status == "succeeded"] == [
        "transform_dataset",
        "trend_analysis",
    ]
    assert calls[1].args["dataset_ref"] != ref
    assert len(tasks.list_evidence(done["run_id"])) == 2


def test_exact_aggregate_sqlite_report_tool_and_real_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import weasyprint

    ref = save_dataframe(
        pd.DataFrame(
            {"group": ["A", "A"], "amount": pd.Series([2**53, 1], dtype="int64")}
        )
    )
    args = {
        "dataset_ref": ref,
        "group_col": "group",
        "value_col": "amount",
        "agg": "sum",
    }
    result = json.loads(json.dumps(aggregate_preview(args)))
    store = SessionStore(str(tmp_path / "exact.db"))
    project = store.create_project("合成精确值")
    conv = store.create_conversation(project.id)
    store.register_dataset(
        ref=ref, project_id=project.id, filename="synthetic.csv", profile={}
    )
    message = store.append_message(
        conversation_id=conv.id, role="assistant", content="汇总"
    )
    store.create_artifact(
        conversation_id=conv.id,
        message_id=message.id,
        type="table",
        payload=result,
        source_tool="aggregate_preview",
        params={**args, "analysis_id": "exact"},
        dataset_ref=ref,
    )
    artifact = SessionStore(store.db_path).list_artifacts(conv.id)[0]
    assembled, _ = _assemble_report_args(None, [artifact])
    server = build_server()
    markdown = server._tools["gen_report_md"].invoke({"title": "精确汇总", **assembled})
    assert markdown["markdown"].count("9007199254740993") == 2
    assert "exact_number" not in markdown["markdown"]
    visible: list[str] = []
    original_html = weasyprint.HTML

    class CapturedHTML:
        def __init__(self, **kwargs: Any) -> None:
            self.html = original_html(**kwargs)

        def write_pdf(self) -> bytes:
            document = self.html.render()
            for page in document.pages:
                visible.extend(
                    box.text
                    for box in page._page_box.descendants()
                    if hasattr(box, "text")
                )
            return document.write_pdf()

    monkeypatch.setattr(weasyprint, "HTML", CapturedHTML)
    pdf = server._tools["export_pdf"].invoke({"report_id": markdown["report_id"]})
    assert Path(pdf["pdf_path"]).read_bytes().startswith(b"%PDF-")
    assert " ".join(visible).count("9007199254740993") == 2
    assert "exact_number" not in " ".join(visible)
