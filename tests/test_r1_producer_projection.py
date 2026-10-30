"""R1 独立生产链反例：真实工具、临时 SQLite 与完整完成校验。

固定 oracle 来自本文件五行输入，不使用被测提取器或投影生成器计算期待。
本文件可直接复制到 tests/；单独执行时加载仓库 tests.conftest 的隔离环境。
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.verifier import verify_completion
from jsonschema import Draft202012Validator
from mcp_servers.common.catalog import tool_output_schema
from mcp_servers.dataset_ops.tools import transform_dataset
from packages.common.dataset_store import load_dataframe, save_dataframe
from packages.orchestration.contracts import TaskContract
from packages.session.store import SessionStore
from packages.session.task_models import EvidenceRecord, TaskRun, ToolInvocation
from packages.session.task_store import TaskStore


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("R1 producer tests forbid all network requests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@dataclass
class Workspace:
    session: SessionStore
    tasks: TaskStore
    contract: TaskContract
    run: TaskRun
    source_ref: str
    project_id: str
    conversation_id: str
    assistant_id: str
    commits: int = 0


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    # Five rows, one B row, one missing value, one complete duplicate pair.
    # Filter A -> 4; drop metric null -> 3; full-row dedup -> 2.
    frame = pd.DataFrame(
        {
            "频道": ["A", "A", "A", "A", "B"],
            "时长": [10.0, 10.0, None, 20.0, 30.0],
            "备注": ["one", "one", "missing", "two", "three"],
        }
    )
    source_ref = save_dataframe(frame)
    session = SessionStore(str(tmp_path / "independent-r1.sqlite3"))
    project = session.create_project("R1 synthetic producer test")
    session.register_dataset(
        ref=source_ref,
        project_id=project.id,
        filename="synthetic.csv",
        profile={"columns": [{"name": key} for key in frame.columns]},
    )
    conversation = session.create_conversation(project.id)
    _, message = session.start_user_turn(
        conversation_id=conversation.id,
        content="核对工具产生的记录数量",
        suggested_title="R1 independent",
    )
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="independent-r1-producer",
        user_text="核对工具产生的记录数量",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 8},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    assistant = session.append_message(
        conversation_id=conversation.id, role="assistant", content="执行合成变换"
    )
    return Workspace(
        session, tasks, contract, run, source_ref, project.id, conversation.id, assistant.id
    )


def commit_transform(
    ws: Workspace,
    operations: dict[str, Any],
    *,
    dataset_ref: str | None = None,
    caller_summary: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ToolInvocation, EvidenceRecord]:
    args = {"dataset_ref": dataset_ref or ws.source_ref, **operations}
    ws.commits += 1
    invocation, _ = ws.tasks.start_invocation(
        run_id=ws.run.run_id,
        tool_call_id=f"transform-{ws.commits}",
        tool_name="transform_dataset",
        arguments=args,
        idempotency_key=f"r1-independent-{ws.commits}",
    )
    result = transform_dataset(args)
    Draft202012Validator(tool_output_schema("transform_dataset")).validate(result)
    # Same Host-owned registration shape as _register_transformed_dataset;
    # never fabricate successful TaskRun, Evidence or tool output.
    ws.session.register_dataset(
        ref=result["dataset_ref"],
        project_id=ws.project_id,
        filename="synthetic-derived.csv",
        profile={"columns": [{"name": key} for key in result["columns"]]},
        parent_ref=result["parent_ref"],
        transform=result["transform"],
    )
    summary = (
        caller_summary
        if caller_summary is not None
        else build_evidence_summary(
            summary="合成工具结果", result=result, artifact_id=None, arguments=args
        )
    )
    ws.run, completed, evidence, _, _, _ = ws.tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=ws.run.state_version,
        assistant_message_id=ws.assistant_id,
        result=result,
        evidence_kind="tool_result",
        evidence_source={"tool": "transform_dataset", "dataset_ref": args["dataset_ref"]},
        evidence_summary=summary,
        artifact_draft=None,
    )
    return result, completed, evidence


def check_answer(
    ws: Workspace,
    text: str,
    invocation: ToolInvocation,
    evidence: EvidenceRecord,
) -> Any:
    return verify_completion(
        contract=ws.contract,
        final_text=text,
        artifacts=[],
        invocations=[invocation],
        evidence=[evidence],
        dataset_bindings=ws.tasks.list_dataset_bindings(ws.run.run_id),
    )


def projection_digest(evidence: EvidenceRecord) -> str:
    """Independent implementation of the documented canonical projection hash."""
    projection = evidence.summary["fact_projection"]
    envelope = {key: value for key, value in projection.items() if key != "projection_hash"}
    paths = projection["paths"]
    facts = [item for item in evidence.summary["value_index"] if item["path"] in paths]
    canonical = json.dumps(
        {"envelope": envelope, "facts": facts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_real_sequential_transform_lineage_and_delta(workspace: Workspace) -> None:
    operations = [
        {"filters": [{"column": "频道", "op": "==", "value": "A"}]},
        {"drop_nulls": ["时长"]},
        {"drop_duplicates": []},
    ]
    texts = [
        "筛选前5行，筛选后4行，删除1行。",
        "去空前4行，去空后3行，删除1行。",
        "去重前3行，去重后2行，删除1行。",
    ]
    parent = workspace.source_ref
    for step, (operation, before, after, text) in enumerate(
        zip(operations, [5, 4, 3], [4, 3, 2], texts, strict=True)
    ):
        result, invocation, evidence = commit_transform(workspace, operation, dataset_ref=parent)
        assert (result["rows_before"], result["rows_after"]) == (before, after)
        assert len(load_dataframe(result["dataset_ref"])) == after
        assert result["parent_ref"] == parent
        assert result["row_changes"]["removed_count"] == 1
        assert result["row_changes"]["steps"][0]["removed_count"] == 1
        projection = evidence.summary["fact_projection"]
        assert projection["input_ref"] == parent
        assert projection["output_ref"] == result["dataset_ref"]
        assert projection["invocation_id"] == invocation.invocation_id
        assert projection["result_hash"] == invocation.result_hash == evidence.result_hash
        assert projection["projection_hash"] == projection_digest(evidence)
        verdict = check_answer(workspace, text, invocation, evidence)
        assert verdict.passed, (step, text, verdict)
        parent = result["dataset_ref"]
    assert len(load_dataframe(workspace.source_ref)) == 5


def test_multiple_operations_have_independent_step_deltas(workspace: Workspace) -> None:
    result, invocation, evidence = commit_transform(
        workspace,
        {
            "filters": [{"column": "频道", "op": "==", "value": "A"}],
            "drop_nulls": ["时长"],
            "drop_duplicates": [],
            "sort": [{"column": "时长", "order": "desc"}],
        },
    )
    assert result["row_changes"]["schema_version"] == 1
    assert result["row_changes"]["removed_count"] == 3
    assert [step["operation"] for step in result["row_changes"]["steps"]] == [
        "filters",
        "drop_nulls",
        "drop_duplicates",
        "sort",
    ]
    assert [
        (step["rows_before"], step["rows_after"], step["removed_count"])
        for step in result["row_changes"]["steps"]
    ] == [(5, 4, 1), (4, 3, 1), (3, 2, 1), (2, 2, 0)]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("处理前5行，处理后2行，删除3行。", True),
        ("筛选后4行，去空后3行，去重后2行，排序后2行。", True),
        ("筛选删除1行，去空删除1行，去重删除1行，排序删除0行。", True),
        ("去重删除3行。", False),
        ("筛选后5行。", False),
        ("去空后2行。", False),
    ],
)
def test_multiple_operations_delivery(workspace: Workspace, text: str, expected: bool) -> None:
    _, invocation, evidence = commit_transform(
        workspace,
        {
            "filters": [{"column": "频道", "op": "==", "value": "A"}],
            "drop_nulls": ["时长"],
            "drop_duplicates": [],
            "sort": [{"column": "时长", "order": "desc"}],
        },
    )
    verdict = check_answer(workspace, text, invocation, evidence)
    diagnostic = extract_numeric_claims(
        final_text=text, goal=workspace.contract.goal, evidence=[evidence]
    )
    assert verdict.passed is expected, (text, verdict, diagnostic)


@pytest.mark.parametrize("columns,removed,after", [([], 1, 4), (["频道"], 3, 2)])
def test_dedup_basis_and_columns_are_preserved(
    workspace: Workspace, columns: list[str], removed: int, after: int
) -> None:
    result, _, evidence = commit_transform(workspace, {"drop_duplicates": columns})
    step = result["row_changes"]["steps"][0]
    assert (step["removed_count"], step["rows_after"]) == (removed, after)
    assert step["count_basis"] == "duplicate_excess"
    assert step["columns"] == columns
    removed_fact = next(
        item
        for item in evidence.summary["value_index"]
        if item["path"] == "$.row_changes.removed_count"
    )
    assert removed_fact["fact"]["basis"] == "duplicate_excess"
    assert removed_fact["fact"]["columns"] == columns


@pytest.mark.parametrize(
    "columns,text,expected",
    [
        ([], "整行去重删除1行。", True),
        (["频道"], "按频道去重删除3行。", True),
        (["频道"], "整行去重删除3行。", False),
        (["频道"], "按时长去重删除3行。", False),
    ],
)
def test_dedup_statement_must_match_operation_basis(
    workspace: Workspace, columns: list[str], text: str, expected: bool
) -> None:
    _, invocation, evidence = commit_transform(workspace, {"drop_duplicates": columns})
    verdict = check_answer(workspace, text, invocation, evidence)
    assert verdict.passed is expected, (text, verdict)


def test_sort_has_explicit_valid_zero_delta(workspace: Workspace) -> None:
    result, invocation, evidence = commit_transform(
        workspace, {"sort": [{"column": "时长", "order": "asc"}]}
    )
    assert result["rows_before"] == result["rows_after"] == 5
    assert result["row_changes"]["removed_count"] == 0
    assert result["row_changes"]["steps"][0]["removed_count"] == 0
    assert check_answer(workspace, "排序前5行，排序后5行，删除0行。", invocation, evidence).passed
    assert not check_answer(workspace, "排序删除1行。", invocation, evidence).passed


def test_untrusted_caller_index_is_replaced_by_real_output(workspace: Workspace) -> None:
    forged = {
        "summary": "故意伪造的调用方摘要",
        "numeric_context": {"dataset_ref": "forged", "data_version_hash": "forged"},
        "value_index": [
            {"path": "$.rows_before", "value": "999", "source_kind": "number"},
            {"path": "$.row_changes.removed_count", "value": "888", "source_kind": "number"},
        ],
        "fact_projection": {"schema": "forged", "projection_hash": "forged"},
    }
    _, invocation, evidence = commit_transform(
        workspace,
        {"filters": [{"column": "频道", "op": "==", "value": "A"}]},
        caller_summary=forged,
    )
    values = {item["path"]: item["value"] for item in evidence.summary["value_index"]}
    assert values["$.rows_before"] == "5"
    assert values["$.rows_after"] == "4"
    assert values["$.row_changes.removed_count"] == "1"
    assert evidence.summary["numeric_context"]["dataset_ref"] == workspace.source_ref
    assert evidence.summary["fact_projection"]["projection_hash"] == projection_digest(evidence)
    assert check_answer(workspace, "筛选前5行，筛选后4行。", invocation, evidence).passed
    assert not check_answer(workspace, "筛选前999行，删除888行。", invocation, evidence).passed


def test_persisted_projection_survives_independent_store_reopen(workspace: Workspace) -> None:
    _, invocation, evidence = commit_transform(
        workspace, {"filters": [{"column": "频道", "op": "==", "value": "A"}]}
    )
    reopened_session = SessionStore(workspace.session.db_path)
    reopened = TaskStore(reopened_session.db_path)
    loaded = reopened.list_evidence(workspace.run.run_id)
    assert loaded == [evidence]
    assert reopened.list_invocations(workspace.run.run_id) == [invocation]
    assert loaded[0].summary["fact_projection"]["projection_hash"] == projection_digest(loaded[0])
    assert check_answer(workspace, "筛选前5行，筛选后4行。", invocation, loaded[0]).passed


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "projection_hash",
        "projection_value",
        "projection_branch",
        "context_branch",
        "projection_input",
        "projection_output",
        "projection_run",
        "projection_invocation",
        "projection_tool",
        "projection_result_hash",
        "evidence_run",
        "evidence_invocation",
        "evidence_tool",
        "evidence_result_hash",
        "invocation_tool",
        "invocation_result_hash",
        "invocation_dataset",
        "projection_null",
        "projection_list",
    ],
)
def test_full_verifier_rejects_tampered_or_unknown_projection(
    workspace: Workspace, mutation: str
) -> None:
    _, invocation, evidence = commit_transform(
        workspace, {"filters": [{"column": "频道", "op": "==", "value": "A"}]}
    )
    summary = copy.deepcopy(evidence.summary)
    projection = summary["fact_projection"]
    envelope_mutations = {
        "schema": "schema",
        "projection_hash": "projection_hash",
        "projection_branch": "branch_hash",
        "projection_input": "input_ref",
        "projection_output": "output_ref",
        "projection_run": "run_id",
        "projection_invocation": "invocation_id",
        "projection_tool": "producer",
        "projection_result_hash": "result_hash",
    }
    if mutation in envelope_mutations:
        projection[envelope_mutations[mutation]] = "unknown-or-other"
    elif mutation == "projection_value":
        summary["value_index"][0]["value"] = "999"
    elif mutation == "context_branch":
        summary["numeric_context"]["data_version_hash"] = "other-version"
    elif mutation == "projection_null":
        summary["fact_projection"] = None
    elif mutation == "projection_list":
        summary["fact_projection"] = []
    elif mutation == "evidence_run":
        evidence = replace(evidence, run_id="other-run")
    elif mutation == "evidence_invocation":
        evidence = replace(evidence, invocation_id="other-call")
    elif mutation == "evidence_tool":
        evidence = replace(evidence, source={**evidence.source, "tool": "aggregate_preview"})
    elif mutation == "evidence_result_hash":
        evidence = replace(evidence, result_hash="other-result")
    elif mutation == "invocation_tool":
        invocation = replace(invocation, tool_name="aggregate_preview")
    elif mutation == "invocation_result_hash":
        invocation = replace(invocation, result_hash="other-result")
    elif mutation == "invocation_dataset":
        invocation = replace(invocation, args={**invocation.args, "dataset_ref": "other-dataset"})
    evidence = replace(evidence, summary=summary)
    verdict = check_answer(workspace, "筛选前5行，筛选后4行。", invocation, evidence)
    assert not verdict.passed, (mutation, verdict)


@pytest.mark.parametrize("bad_projection", [None, {}, [], {"schema": "future-version"}])
def test_unknown_projection_cannot_fall_back_to_legacy_equal_values(
    workspace: Workspace, bad_projection: Any
) -> None:
    _, _, evidence = commit_transform(
        workspace, {"filters": [{"column": "频道", "op": "==", "value": "A"}]}
    )
    summary = copy.deepcopy(evidence.summary)
    summary["fact_projection"] = bad_projection
    evidence = replace(evidence, summary=summary)
    claims = extract_numeric_claims(
        final_text="筛选前5行，筛选后4行。", goal=workspace.contract.goal, evidence=[evidence]
    )
    refs = [ref for claim in claims for ref in claim.value_refs]
    assert refs, "不能通过丢失所有数量声明制造拒绝/通过"
    assert any(ref.get("supported") is not True for ref in refs)
