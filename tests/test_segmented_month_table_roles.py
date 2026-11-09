"""Typed group cells cannot become metric names in the quantity binder.

Actual deterministic profile/aggregate tools, immutable files, SQLite seals and
full Verifier; no model. Twelve synthetic rows independently sum to 21 and 57.
"""

import json
from dataclasses import asdict

import pandas as pd
import pytest
from apps.orchestrator.agent_tools import _profile_with_quality
from apps.orchestrator.control.claims import build_evidence_summary, extract_claims
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.dataset_ops.tools import aggregate_preview
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.orchestration.analysis_requirements import ColumnRoleBinding
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.dataset_scope import operation_admission_error
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_tools import build_excel
from tests.test_r2_operation_completion import no_network

__all__ = ["no_network"]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("correct", True),
        ("wrong-value", False),
        ("wrong-field", False),
        ("wrong-month", False),
        ("wrong-dataset", False),
        ("wrong-version", False),
        ("wrong-scope", False),
        ("unknown-unit", False),
        ("correct-version", True),
        ("correct-scope", True),
        ("wrong-first-table-version", False),
        ("wrong-second-table-scope", False),
    ],
)
def test_sealed_month_table_group_metric_and_unit_roles(tmp_path, mutation, expected):
    metric = "销售额（元）"
    frame = pd.DataFrame({"月份": ["2026-01"] * 6 + ["2026-02"] * 6, metric: range(1, 13)})
    ref = save_dataframe(frame)
    session = SessionStore(str(tmp_path / "month-table.sqlite3"))
    project = session.create_project("independent month table role oracle")
    session.register_dataset(
        ref=ref,
        project_id=project.id,
        filename="synthetic-month.csv",
        profile=infer_schema({"dataset_ref": ref}).to_dict(),
    )
    conversation = session.create_conversation(project.id)
    goal = f"按月份汇总{metric}。"
    _, message = session.start_user_turn(
        conversation_id=conversation.id, content=goal, suggested_title="month table"
    )
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="month-table-roles",
        user_text=goal,
        chart_required=False,
        report_required=False,
        pdf_required=False,
        columns=tuple(frame.columns),
        dataset_refs=(ref,),
        column_roles=(ColumnRoleBinding("月份", "dimension"), ColumnRoleBinding(metric, "metric")),
    )
    assert not contract.analysis_requirements.needs_clarification()
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    assistant = session.append_message(
        conversation_id=conversation.id, role="assistant", content="读取并汇总合成数据"
    )
    results = {}
    for number, (tool, args) in enumerate(
        [
            ("get_data_profile", {"dataset_ref": ref}),
            (
                "aggregate_preview",
                {"dataset_ref": ref, "group_col": "月份", "value_col": metric, "agg": "sum"},
            ),
        ]
    ):
        assert (
            operation_admission_error(
                contract.analysis_requirements,
                run_id=run.run_id,
                tool_name=tool,
                arguments=args,
                invocations=tasks.list_invocations(run.run_id),
                evidence=tasks.list_evidence(run.run_id),
                bindings=tasks.list_dataset_bindings(run.run_id),
            )
            is None
        )
        invocation, _ = tasks.start_invocation(
            run_id=run.run_id,
            tool_call_id=f"real-{number}",
            tool_name=tool,
            arguments=args,
            idempotency_key=f"real-{number}",
        )
        result = (
            _profile_with_quality(build_excel(), args)
            if tool == "get_data_profile"
            else aggregate_preview(args)
        )
        results[tool] = result
        run, *_ = tasks.commit_tool_success(
            invocation.invocation_id,
            expected_version=run.state_version,
            assistant_message_id=assistant.id,
            result=result,
            evidence_kind="tool_result",
            evidence_source={"tool": tool, "dataset_ref": ref},
            evidence_summary=build_evidence_summary(
                summary="", result=result, artifact_id=None, arguments=args
            ),
            artifact_draft=None,
        )
    assert {row["group"]: row["value"] for row in results["aggregate_preview"]["rows"]} == {
        "2026-01": 21,
        "2026-02": 57,
    }
    reopened = TaskStore(session.db_path)
    evidence = reopened.list_evidence(run.run_id)
    version = evidence[-1].summary["fact_projection"]["branch_hash"]
    table = f"| 月份 | {metric} |\n|---|---|\n| 2026-01 | 21 |\n| 2026-02 | 57 |"
    text = table + f"\n\n{metric}总计78。"
    second_table_start = None
    if mutation == "wrong-value":
        text = text.replace("| 21 |", "| 22 |")
    elif mutation == "wrong-field":
        text = text.replace(metric, "库存（元）")
    elif mutation == "wrong-month":
        text = text.replace("2026-01", "2026-03")
    elif mutation == "wrong-dataset":
        text = "数据集：" + "b" * 32 + "。\n" + text
    elif mutation == "wrong-version":
        text = "数据版本：" + "b" * 64 + "。\n" + text
    elif mutation == "wrong-scope":
        text = "筛选条件：月份=2026-02。\n" + text
    elif mutation == "unknown-unit":
        text = text.replace("| 21 |", "| 21桶 |")
    elif mutation == "correct-version":
        text = f"数据版本：{version}。\n" + text
    elif mutation == "correct-scope":
        text = "统计范围：全表。\n" + text
    elif mutation == "wrong-first-table-version":
        prefix = "数据版本：" + "b" * 64 + "。\n" + table + "\n\n"
        second_table_start = len(prefix)
        text = prefix + text
    elif mutation == "wrong-second-table-scope":
        prefix = table + "\n\n筛选条件：月份=2026-02。\n"
        second_table_start = len(prefix)
        text = prefix + text
    claims = extract_claims(final_text=text, goal=goal, evidence=evidence)
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=reopened.list_invocations(run.run_id),
        evidence=evidence,
        dataset_bindings=reopened.list_dataset_bindings(run.run_id),
    )
    (tmp_path / "sealed-month-table.json").write_text(
        json.dumps(
            {
                "mutation": mutation,
                "expected": expected,
                "text": text,
                "results": results,
                "contract": contract.to_dict(),
                "evidence": [asdict(e) for e in evidence],
                "claims": [asdict(c) for c in claims],
                "verdict": asdict(verdict),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    assert verdict.passed is expected
    refs = [r for c in claims for r in c.value_refs if r.get("token") in {"21", "57"}]
    if expected:
        assert len(refs) == 2 and all(r["supported"] for r in refs)
        assert all(r["semantic_binding"]["metric"] == metric for r in refs)
        assert all(text[slice(*r["source_span"])] == r["source_token"] for r in refs)
    if second_table_start is not None:
        first = [r for r in refs if r["source_span"][0] < second_table_start]
        second = [r for r in refs if r["source_span"][0] >= second_table_start]
        assert len(first) == len(second) == 2
        first_supported = mutation == "wrong-second-table-scope"
        assert all(r["supported"] is first_supported for r in first)
        assert all(r["supported"] is (not first_supported) for r in second)
