"""R2 operation oracle with real deterministic tools and isolated SQLite.

The five-row input independently requires 5 -> 4 -> 3 -> 2 and cleaned sum30.
Wrong traces are deliberate isolated tool-layer fault fixtures, not Agent PASS.
No verifier output is used to choose expected parameters or outcomes.
"""

from __future__ import annotations

import json
import socket
from dataclasses import asdict, replace

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.dataset_ops.tools import aggregate_preview, transform_dataset
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import load_dataframe, save_dataframe
from packages.orchestration.analysis_requirements import ColumnRoleBinding
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.dataset_scope import operation_admission_error, operation_progress
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

CHAIN_GOAL = "筛选条件：频道=A。删除时长为空的记录。按整行去重。"
FILTER = {"filters": [{"column": "频道", "op": "==", "value": "A"}]}
NULLS = {"drop_nulls": ["时长"]}
DEDUP = {"drop_duplicates": []}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("R2 operation tests never call network or a model")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


class RealTrace:
    def __init__(self, tmp_path, goal):
        frame = pd.DataFrame(
            {
                "频道": ["A", "A", "A", "A", "B"],
                "时长": [10.0, 10.0, None, 20.0, 30.0],
                "备注": ["one", "one", "missing", "two", "three"],
            }
        )
        self.source = self.current = save_dataframe(frame)
        self.session = SessionStore(str(tmp_path / "operations.sqlite3"))
        self.project = self.session.create_project("synthetic R2 operation oracle")
        self.session.register_dataset(
            ref=self.source,
            project_id=self.project.id,
            filename="synthetic.csv",
            profile=infer_schema({"dataset_ref": self.source}).to_dict(),
        )
        self.conversation = self.session.create_conversation(self.project.id)
        _, message = self.session.start_user_turn(
            conversation_id=self.conversation.id, content=goal, suggested_title="R2 operation"
        )
        self.tasks = TaskStore(self.session.db_path)
        self.contract = build_minimal_contract(
            run_id="r2-operation-proof",
            user_text=goal,
            chart_required=False,
            report_required=False,
            pdf_required=False,
            columns=tuple(frame.columns),
            dataset_refs=(self.source,),
            # Explicit role fixture for compiler-level policy. Actual Host role
            # inference and clarification are covered in separate real paths.
            column_roles=(
                ColumnRoleBinding("频道", "dimension"),
                ColumnRoleBinding("时长", "metric"),
                ColumnRoleBinding("备注", "dimension"),
            ),
        )
        self.run, _ = self.tasks.create_run(
            project_id=self.project.id,
            conversation_id=self.conversation.id,
            user_message_id=message.id,
            contract=self.contract,
            budget={"max_tool_calls": 8},
        )
        self.run, _ = self.tasks.transition(
            self.run.run_id,
            expected_version=self.run.state_version,
            status="running",
            event_type="run.started",
            payload={},
        )
        self.assistant = self.session.append_message(
            conversation_id=self.conversation.id, role="assistant", content="处理合成数据"
        )
        self.count = 0

    def state(self):
        # Reload through a different TaskStore to exercise persistence.
        tasks = TaskStore(self.session.db_path)
        return (
            tasks.list_invocations(self.run.run_id),
            tasks.list_evidence(self.run.run_id),
            tasks.list_dataset_bindings(self.run.run_id),
        )

    def admit(self, operations, *, tool="transform_dataset", source=None):
        invocations, evidence, bindings = self.state()
        return operation_admission_error(
            self.contract.analysis_requirements,
            run_id=self.run.run_id,
            tool_name=tool,
            arguments={"dataset_ref": source or self.current, **operations},
            invocations=invocations,
            evidence=evidence,
            bindings=bindings,
        )

    def execute(self, operations, *, tool="transform_dataset", source=None):
        args = {"dataset_ref": source or self.current, **operations}
        self.count += 1
        invocation, _ = self.tasks.start_invocation(
            run_id=self.run.run_id,
            tool_call_id=f"tool-{self.count}",
            tool_name=tool,
            arguments=args,
            idempotency_key=f"step-{self.count}",
        )
        result = (transform_dataset if tool == "transform_dataset" else aggregate_preview)(args)
        if tool == "transform_dataset":
            self.current = result["dataset_ref"]
            self.session.register_dataset(
                ref=self.current,
                project_id=self.project.id,
                filename="synthetic-child.csv",
                profile=infer_schema({"dataset_ref": self.current}).to_dict(),
                parent_ref=result["parent_ref"],
                transform=result["transform"],
            )
        self.run, *_ = self.tasks.commit_tool_success(
            invocation.invocation_id,
            expected_version=self.run.state_version,
            assistant_message_id=self.assistant.id,
            result=result,
            evidence_kind="tool_result",
            evidence_source={"tool": tool, "dataset_ref": args["dataset_ref"]},
            evidence_summary=build_evidence_summary(
                summary="", result=result, artifact_id=None, arguments=args
            ),
            artifact_draft=None,
        )
        return result

    def verify(self, text="已完成所需处理。", *, state=None):
        invocations, evidence, bindings = state or self.state()
        return verify_completion(
            contract=self.contract,
            final_text=text,
            artifacts=[],
            invocations=invocations,
            evidence=evidence,
            dataset_bindings=bindings,
        )


def test_real_ordered_trace_and_reopened_lineage(tmp_path):
    trace = RealTrace(tmp_path, CHAIN_GOAL)
    for parameters, expected_rows in [(FILTER, 4), (NULLS, 3), (DEDUP, 2)]:
        assert trace.admit(parameters) is None
        trace.execute(parameters)
        assert len(load_dataframe(trace.current)) == expected_rows
    assert float(load_dataframe(trace.current)["时长"].sum()) == 30
    result = trace.verify()
    assert result.passed, result
    invocations, evidence, bindings = trace.state()
    progress = operation_progress(
        trace.contract.analysis_requirements, trace.run.run_id, invocations, evidence, bindings
    )
    assert progress.complete and len(progress.completed) == 3
    assert progress.scope == {trace.current}
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {
                "contract": trace.contract.to_dict(),
                "progress": asdict(progress),
                "invocations": [asdict(i) for i in invocations],
                "evidence": [asdict(e) for e in evidence],
                "business_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
            default=list,
        )
    )


@pytest.mark.parametrize("fault", ["omit", "reverse", "wrong_column", "combined"])
def test_real_wrong_operation_traces_never_complete(tmp_path, fault):
    trace = RealTrace(tmp_path, CHAIN_GOAL)
    if fault == "reverse":
        trace.execute(DEDUP)
        trace.execute(FILTER)
        trace.execute(NULLS)
    else:
        trace.execute(FILTER)
        if fault == "combined":
            trace.execute({**NULLS, **DEDUP})
        elif fault == "wrong_column":
            trace.execute({"drop_nulls": ["备注"]})
            trace.execute(DEDUP)
        else:
            trace.execute(NULLS)
    result = trace.verify()
    assert not result.passed, result
    assert any(
        i.code in {"required_operation_not_satisfied", "analysis_operation_mismatch"}
        for i in result.issues
    )


@pytest.mark.parametrize("fault", ["old_version", "lineage", "result_hash", "arguments_hash"])
def test_real_proof_cannot_be_replaced_by_wrong_identity(tmp_path, fault):
    trace = RealTrace(tmp_path, CHAIN_GOAL)
    for parameters in (FILTER, NULLS, DEDUP):
        trace.execute(parameters)
    invocations, evidence, bindings = trace.state()
    if fault == "old_version":
        invocations[-1] = replace(
            invocations[-1], args={**invocations[-1].args, "dataset_ref": trace.source}
        )
    elif fault == "lineage":
        bindings = [b for b in bindings if b.dataset_ref != trace.current]
    elif fault == "result_hash":
        evidence[-1] = replace(evidence[-1], result_hash="f" * 64)
    else:
        invocations[-1] = replace(invocations[-1], args_hash="f" * 64)
    assert not trace.verify(state=(invocations, evidence, bindings)).passed


@pytest.mark.parametrize(
    "group,answer,expected",
    [
        ("频道", "时长总计70。", True),
        ("备注", "时长总计70。", False),
        ("频道", "已完成汇总。", False),
    ],
)
def test_group_and_total_are_separate_real_obligations(tmp_path, group, answer, expected):
    trace = RealTrace(tmp_path, "按频道汇总时长并计算总计。")
    result = trace.execute(
        {"group_col": group, "value_col": "时长", "agg": "sum"}, tool="aggregate_preview"
    )
    assert result["total_value"] == 70
    verified = trace.verify(answer)
    assert verified.passed is expected, verified


def test_no_operations_or_plan_text_cannot_fulfil_required_operations(tmp_path):
    trace = RealTrace(tmp_path, CHAIN_GOAL)
    assert not trace.verify("所需处理已全部完成。").passed


@pytest.mark.parametrize(
    "goal,parameters,expected",
    [
        ("本轮只画像，暂不筛选或清洗。", FILTER, False),
        ("本轮只画像，暂不筛选或清洗。", NULLS, False),
        ("本轮只画像，暂不筛选或清洗。", DEDUP, False),
        ("不要去重。", DEDUP, False),
        ("不要去重。", NULLS, True),
    ],
)
def test_typed_noncurrent_family_does_not_authorize_or_expand_other_bans(
    tmp_path, goal, parameters, expected
):
    trace = RealTrace(tmp_path, goal)
    assert (trace.admit(parameters) is None) is expected


@pytest.mark.parametrize("value,expected", [(True, False), (1.0, True)])
def test_filter_argument_equality_preserves_boolean_identity(tmp_path, value, expected):
    trace = RealTrace(tmp_path, "筛选时长=1。")
    parameters = {"filters": [{"column": "时长", "op": "==", "value": value}]}
    assert (trace.admit(parameters) is None) is expected
