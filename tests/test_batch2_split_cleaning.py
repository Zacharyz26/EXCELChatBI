"""完整筛选后的多步清洗必须逐边保留同一来源和条件。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from apps.orchestrator.agent_loop import _analysis_scope_error
from packages.orchestration.analysis_requirements import AnalysisRequirements
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.dataset_scope import verified_analysis_scope
from packages.session.task_models import TaskDatasetBinding

from tests.test_batch1_contracts import evidence, invocation

D, F, X, Y, OTHER = (str(n) * 32 for n in range(1, 6))
FILTERS = [
    {"column": "设备", "op": "==", "value": "A"},
    {"column": "状态", "op": "==", "value": "运行"},
]


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "reversed_edges",
        "lost_condition",
        "extra_filter",
        "replaced_filter",
        "wrong_source",
        "missing_first_evidence",
        "missing_middle_evidence",
        "bad_hash",
        "failed_middle",
        "old_middle",
        "wrong_parent",
        "missing_binding",
        "wrong_producer",
        "unsupported_operation",
        "self_cycle",
        "unrooted_cycle",
    ],
)
def test_filtered_cleanup_scope_requires_every_verified_edge(mutation: str) -> None:
    args = [
        {"dataset_ref": D, "filters": list(FILTERS)},
        {"dataset_ref": F, "drop_nulls": ["停机分钟"]},
        {"dataset_ref": X, "drop_duplicates": []},
    ]
    if mutation == "lost_condition":
        args[0]["filters"] = FILTERS[:1]
    if mutation == "extra_filter":
        args[1]["filters"] = FILTERS[:1]
    if mutation == "replaced_filter":
        args[1]["filters"] = [{"column": "设备", "op": "==", "value": "B"}]
    if mutation == "wrong_source":
        args[1]["dataset_ref"] = OTHER
    if mutation == "unsupported_operation":
        args[1]["rename_columns"] = {"设备": "other"}
    if mutation == "self_cycle":
        args[1]["dataset_ref"] = X
    if mutation == "unrooted_cycle":
        args[1]["dataset_ref"] = Y
    refs = [F, X, Y]
    tools = [invocation("transform_dataset", a, str(i)) for i, a in enumerate(args)]
    records = [
        evidence({"dataset_ref": ref}, a, str(i))
        for i, (a, ref) in enumerate(zip(args, refs, strict=True))
    ]
    bindings = [
        TaskDatasetBinding("r", ref, "derived", a["dataset_ref"], str(i), "now", "now")
        for i, (a, ref) in enumerate(zip(args, refs, strict=True))
    ]
    if mutation == "missing_first_evidence":
        records = records[1:]
    if mutation == "missing_middle_evidence":
        records = [records[0], records[2]]
    if mutation == "bad_hash":
        records[1] = replace(records[1], result_hash="other")
    if mutation == "failed_middle":
        tools[1] = replace(tools[1], status="failed")
    if mutation == "old_middle":
        bindings[1] = replace(bindings[1], run_id="old")
    if mutation == "wrong_parent":
        bindings[1] = replace(bindings[1], parent_ref=OTHER)
    if mutation == "missing_binding":
        bindings.pop(1)
    if mutation == "wrong_producer":
        tools[1] = replace(tools[1], tool_name="join_datasets")
    if mutation == "reversed_edges":
        tools.reverse()
        records.reverse()
        bindings.reverse()
    requirements = AnalysisRequirements(filters=tuple(FILTERS), dataset_ref=D)
    scope = verified_analysis_scope(requirements, "r", tools, records, bindings)
    assert (Y in scope) is (mutation in {"valid", "reversed_edges"})
    assert D not in scope and OTHER not in scope
    if mutation in {"lost_condition", "missing_first_evidence"}:
        assert not scope


@pytest.mark.parametrize(
    "arguments,accepted",
    [
        ({"dataset_ref": F, "drop_nulls": ["停机分钟"]}, True),
        ({"dataset_ref": F, "drop_duplicates": []}, True),
        ({"dataset_ref": F, "drop_nulls": [], "drop_duplicates": []}, True),
        ({"dataset_ref": F, "sort": [{"column": "月份"}]}, True),
        ({"dataset_ref": F, "exclude_row_indices": [0]}, True),
        ({"dataset_ref": D, "filters": FILTERS}, True),
        ({"dataset_ref": D, "drop_nulls": []}, False),
        ({"dataset_ref": OTHER, "drop_duplicates": []}, False),
        ({"dataset_ref": F, "filters": FILTERS[:1], "drop_duplicates": []}, False),
        ({"dataset_ref": F, "filters": [], "drop_duplicates": []}, False),
        ({"dataset_ref": F, "drop_duplicates": [], "fill_nulls": 0}, False),
        ({"dataset_ref": F}, False),
    ],
)
def test_filtered_cleanup_execution_gate(arguments: dict, accepted: bool) -> None:
    first = {"dataset_ref": D, "filters": FILTERS}
    tools = [invocation("transform_dataset", first, "filter")]
    records = [evidence({"dataset_ref": F}, first, "filter")]
    bindings = [TaskDatasetBinding("r", F, "derived", D, "filter", "now", "now")]
    facade = SimpleNamespace(
        list_invocations=lambda _: tools,
        list_evidence=lambda _: records,
        list_dataset_bindings=lambda _: bindings,
    )
    contract = build_minimal_contract(
        run_id="r",
        user_text="筛选设备=A且状态=运行，删除停机分钟空值并去重。",
        dataset_refs=(D,),
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    assert (
        _analysis_scope_error(facade, "r", contract, "transform_dataset", arguments)
        is None
    ) is accepted


@pytest.mark.parametrize("wrong_source", [False, True])
async def test_real_host_preserves_filtered_cleanup_chain(
    tmp_path, wrong_source: bool
) -> None:
    import json

    import pandas as pd
    from apps.orchestrator.agent_loop import (
        AgentLoopConfig,
        ConversationLockPool,
        stream_agent_chat,
    )
    from apps.orchestrator.agent_tools import AgentContext
    from packages.common.dataset_store import save_dataframe
    from packages.models.types import ModelResponse, ToolCall
    from packages.session.store import SessionStore
    from packages.session.task_store import TaskStore

    from tests.test_agent_tools import _registry, build_excel

    store = SessionStore(str(tmp_path / "split.db"))
    project = store.create_project("分步清洗合成验收")
    conversation = store.create_conversation(project.id)
    rows = [
        {"月份": f"2026-{m:02d}", "设备": d, "状态": "运行", "停机分钟": m * v}
        for m in range(1, 7)
        for d, v in [("A", 10), ("B", 20)]
    ]
    rows += [
        dict(rows[0]),
        {"月份": "2026-02", "设备": "A", "状态": "运行", "停机分钟": None},
    ]
    ref = save_dataframe(pd.DataFrame(rows))
    profile = (
        build_excel()._tools["infer_schema"].invoke({"dataset_ref": ref}).to_dict()
    )
    store.register_dataset(
        ref=ref, project_id=project.id, filename="synthetic.csv", profile=profile
    )

    def child(parent):
        return next(
            d.ref for d in store.list_datasets(project.id) if d.parent_ref == parent
        )

    class Gateway:
        turns = 0

        async def stream_turn(self, *args, **kwargs):
            self.turns += 1
            step = self.turns
            if step == 1:
                name, params = (
                    "transform_dataset",
                    {"dataset_ref": ref, "filters": FILTERS},
                )
            elif step == 2:
                name, params = (
                    "transform_dataset",
                    {
                        "dataset_ref": ref if wrong_source else child(ref),
                        "drop_nulls": ["停机分钟"],
                    },
                )
            elif wrong_source:
                raise RuntimeError(
                    "Fixture stops after the rejected operation; no repair"
                )
            elif step == 3:
                name, params = (
                    "transform_dataset",
                    {"dataset_ref": child(child(ref)), "drop_duplicates": []},
                )
            elif step == 4:
                name, params = (
                    "get_data_profile",
                    {"dataset_ref": child(child(child(ref)))},
                )
            elif step == 5:
                name, params = (
                    "aggregate_preview",
                    {
                        "dataset_ref": child(child(child(ref))),
                        "group_col": "月份",
                        "value_col": "停机分钟",
                        "agg": "sum",
                        "sort": "group",
                    },
                )
            elif step == 6:
                name, params = (
                    "gen_chart",
                    {
                        "dataset_ref": child(child(child(ref))),
                        "chart_type": "line",
                        "encoding": {"x": "月份", "y": "停机分钟", "agg": "sum"},
                    },
                )
            elif step == 7:
                yield ModelResponse(
                    content="清洗后 6 行，停机分钟总计 210。", model="offline-split"
                )
                return
            else:
                raise RuntimeError("No fixture retry")
            yield ModelResponse(
                content="",
                model="offline-split",
                tool_calls=[
                    ToolCall(
                        id=f"s{step}",
                        name=name,
                        arguments=json.dumps(params, ensure_ascii=False),
                    )
                ],
            )

    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        events = [
            e
            async for e in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=(
                    f"数据集 {ref}：先筛选设备=A且状态=运行，再去掉停机分钟为空的行，"
                    "然后去重，按月份汇总停机分钟并生成折线图。回答清洗后行数和停机分钟总计。"
                ),
                gateway=Gateway(),
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=7, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
    run_id = next(
        json.loads(e["data"])["run_id"] for e in events if e["event"] == "meta"
    )
    task_store = TaskStore(store.db_path)
    invocations = task_store.list_invocations(run_id)
    if wrong_source:
        assert len(invocations) == 2 and invocations[1].status == "failed"
        assert "筛选参数" in invocations[1].error_text
        assert len(task_store.list_dataset_bindings(run_id)) == 2
        return
    done = next(json.loads(e["data"]) for e in events if e["event"] == "done")
    assert done["run_status"] == "completed"
    assert len(invocations) == 6 and all(i.status == "succeeded" for i in invocations)
    final_ref = child(child(child(ref)))
    artifacts = store.list_artifacts(conversation.id)
    assert {a.dataset_ref for a in artifacts} == {final_ref}
    table = next(a for a in artifacts if a.source_tool == "aggregate_preview")
    assert table.payload["total_value"] == 210
    assert [r["value"] for r in table.payload["rows"]] == [10, 20, 30, 40, 50, 60]
