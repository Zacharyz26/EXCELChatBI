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


def make_sealed_world(tmp_path):
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
    return contract, reopened, run, evidence, metric, ref, version


@pytest.mark.parametrize(
    "variant,passed,table_support",
    [
        ("same-paragraph-exact-identity", True, [True]),
        ("same-paragraph-wrong-version", False, [False]),
        ("same-paragraph-unknown-filter", False, [False]),
        ("two-correct-explicit-sources", True, [True, True]),
        ("wrong-first-correct-second", False, [False, True]),
        ("correct-first-wrong-second", False, [True, False]),
        ("blank-correct-version", True, [True]),
        ("blank-wrong-version", False, None),
        ("blank-unknown-filter", False, None),
    ],
)
def test_independent_table_parent(tmp_path, variant, passed, table_support):
    contract, tasks, run, evidence, metric, ref, version = make_sealed_world(tmp_path)
    table = f"| 月份 | {metric} |\n|---|---|\n| 2026-01 | 21 |\n| 2026-02 | 57 |"
    valid = f"数据集：{ref}，数据版本：{version}。"
    bad = f"数据集：{ref}，数据版本：" + "c" * 64 + "。"
    other = "筛选条件：未知列=A。"
    if variant == "same-paragraph-exact-identity":
        text = valid + "\n" + table
    elif variant == "same-paragraph-wrong-version":
        text = bad + "\n" + table
    elif variant == "same-paragraph-unknown-filter":
        text = other + "\n" + table
    elif variant == "two-correct-explicit-sources":
        text = valid + "\n" + table + "\n\n" + valid + "\n" + table
    elif variant == "wrong-first-correct-second":
        text = bad + "\n" + table + "\n\n" + valid + "\n" + table
    elif variant == "correct-first-wrong-second":
        text = valid + "\n" + table + "\n\n" + bad + "\n" + table
    elif variant == "blank-correct-version":
        text = valid + "\n\n" + table
    elif variant == "blank-wrong-version":
        text = bad + "\n\n" + table
    else:
        text = other + "\n\n" + table
    text += f"\n\n{metric}总计78。"
    claims = extract_claims(final_text=text, goal=contract.goal, evidence=evidence)
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=tasks.list_invocations(run.run_id),
        evidence=evidence,
        dataset_bindings=tasks.list_dataset_bindings(run.run_id),
    )
    refs = [r for c in claims for r in c.value_refs if r.get("token") in {"21", "57"}]
    witness = {
        "variant": variant,
        "text": text,
        "expected_passed": passed,
        "table_support": table_support,
        "evidence": [asdict(e) for e in evidence],
        "refs": refs,
        "claims": [asdict(c) for c in claims],
        "verdict": asdict(verdict),
        "contract": contract.to_dict(),
    }
    (tmp_path / "independent-table-parent.json").write_text(
        json.dumps(witness, ensure_ascii=False, default=str, indent=2)
    )
    assert verdict.passed is passed, witness
    if table_support is not None:
        starts = []
        offset = 0
        while True:
            at = text.find(table, offset)
            if at < 0:
                break
            starts.append(at)
            offset = at + len(table)
        assert len(starts) == len(table_support)
        for start, supported in zip(starts, table_support, strict=True):
            chosen = [
                r
                for r in refs
                if r.get("source_span") and start <= r["source_span"][0] < start + len(table)
            ]
            assert len(chosen) == 2, witness
            assert all(r.get("supported") is supported for r in chosen), witness
            assert all(text[slice(*r["source_span"])] == r["source_token"] for r in chosen), witness
