"""Independent actual-tool/SQLite scope oracle and labeled query-Evidence fixtures."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import load_dataframe, save_dataframe
from packages.orchestration.analysis_requirements import ColumnRoleBinding
from packages.orchestration.contracts import build_minimal_contract
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_r2_operation_completion import RealTrace, no_network  # noqa: F401


class IndependentTrace(RealTrace):
    """Reuse only the transaction harness; values and expected arguments are independent."""

    def __init__(self, tmp_path, goal):
        self.source = self.current = save_dataframe(
            pd.DataFrame(
                {
                    "组别": ["A", "A", "B", "B"],
                    "值": [1.0, 1.0, None, 2.0],
                    "备注": ["x", "x", "missing", "y"],
                }
            )
        )
        self.session = SessionStore(str(tmp_path / "independent-scope.sqlite3"))
        self.project = self.session.create_project("independent synthetic R2 scope")
        self.session.register_dataset(
            ref=self.source,
            project_id=self.project.id,
            filename="four.csv",
            profile=infer_schema({"dataset_ref": self.source}).to_dict(),
        )
        self.conversation = self.session.create_conversation(self.project.id)
        _, message = self.session.start_user_turn(
            conversation_id=self.conversation.id, content=goal, suggested_title="scope holdout"
        )
        self.tasks = TaskStore(self.session.db_path)
        self.contract = build_minimal_contract(
            run_id="independent-scope",
            user_text=goal,
            chart_required=False,
            report_required=False,
            pdf_required=False,
            columns=("组别", "值", "备注"),
            dataset_refs=(self.source,),
            column_roles=(
                ColumnRoleBinding("组别", "dimension"),
                ColumnRoleBinding("值", "metric"),
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
            conversation_id=self.conversation.id, role="assistant", content="验证合成数据"
        )
        self.count = 0
        self.directory = tmp_path

    def search_fixture(self, query):
        """Labeled local query fixture: real invocation/commit, no external KB search."""
        self.count += 1
        arguments = {"query": query}
        invocation, _ = self.tasks.start_invocation(
            run_id=self.run.run_id,
            tool_call_id=f"query-{self.count}",
            tool_name="kb_search",
            arguments=arguments,
            idempotency_key=f"query-{self.count}",
        )
        result = {"query": query, "matches": []}
        self.run, *_ = self.tasks.commit_tool_success(
            invocation.invocation_id,
            expected_version=self.run.state_version,
            assistant_message_id=self.assistant.id,
            result=result,
            evidence_kind="tool_result",
            evidence_source={"tool": "kb_search"},
            evidence_summary=build_evidence_summary(
                summary="local empty-query fixture",
                result=result,
                artifact_id=None,
                arguments=arguments,
            ),
            artifact_draft=None,
        )

    def witness(self, name, result):
        invocations, evidence, bindings = self.state()
        (self.directory / (name + ".json")).write_text(
            json.dumps(
                {
                    "contract": self.contract.to_dict(),
                    "invocations": [asdict(i) for i in invocations],
                    "evidence": [asdict(e) for e in evidence],
                    "bindings": [asdict(b) for b in bindings],
                    "result": asdict(result),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def test_boolean_filter_operand_is_not_numeric_identity_at_admission(tmp_path):
    trace = IndependentTrace(tmp_path, "筛选值=1。")
    assert trace.admit({"filters": [{"column": "值", "op": "==", "value": True}]}) is not None


def test_boolean_filter_operand_cannot_complete_even_when_numbers_coincide(tmp_path):
    trace = IndependentTrace(tmp_path, "筛选值=1。")
    trace.execute({"filters": [{"column": "值", "op": "==", "value": True}]})
    assert len(load_dataframe(trace.current)) == 2  # Actual tool succeeds; not a mock.
    result = trace.verify("处理结束。")
    trace.witness("boolean-filter", result)
    assert not result.passed, result


def test_numeric_filter_positive_uses_the_exact_scalar_identity(tmp_path):
    trace = IndependentTrace(tmp_path, "筛选值=1。")
    assert trace.admit({"filters": [{"column": "值", "op": "==", "value": 1}]}) is None
    trace.execute({"filters": [{"column": "值", "op": "==", "value": 1}]})
    assert trace.verify("处理结束。").passed


@pytest.mark.parametrize("goal", ["不要排除任何记录，仅看画像。", "本轮只画像，暂不筛选或清洗。"])
def test_row_exclusion_cannot_bypass_a_filter_prohibition_by_changing_argument_key(tmp_path, goal):
    trace = IndependentTrace(tmp_path, goal)
    assert trace.admit({"exclude_row_indices": [0]}) is not None


def test_optional_allowed_cleanup_and_full_completion_agree(tmp_path):
    trace = IndependentTrace(tmp_path, "不要去重。")
    assert trace.admit({"drop_nulls": ["值"]}) is None
    trace.execute({"drop_nulls": ["值"]})
    assert len(load_dataframe(trace.current)) == 3
    assert float(load_dataframe(trace.current)["值"].sum()) == 4
    result = trace.verify("处理结束。")
    trace.witness("allowed-optional-cleanup", result)
    assert result.passed, result


@pytest.mark.parametrize(
    "parameters",
    [
        {"filters": [{"column": "组别", "op": "==", "value": "A"}]},
        {"drop_nulls": ["值"]},
        {"drop_duplicates": []},
    ],
)
def test_forbidden_success_cannot_be_laundered_by_final_answer(tmp_path, parameters):
    trace = IndependentTrace(tmp_path, "暂不筛选或清洗。")
    trace.execute(parameters)  # Deliberate isolated bypass, then the real complete verifier.
    assert not trace.verify("处理结束。").passed


def test_two_current_transforms_cannot_be_replaced_by_duplicate_first_success(tmp_path):
    trace = IndependentTrace(tmp_path, "先删除值的空值，再按整行去重。")
    trace.execute({"drop_nulls": ["值"]})
    trace.execute({"drop_nulls": ["值"]})
    assert not trace.verify("处理结束。").passed


@pytest.mark.parametrize(
    "query,expected",
    [
        ("公司规定", True),
        ("公司规定的附录", False),
        ("《公司规定》的附录", False),
        ("公司规定修订版", False),
    ],
)
def test_required_query_uses_exact_object_with_persisted_invocation(tmp_path, query, expected):
    trace = IndependentTrace(tmp_path, "请查询《公司规定》。")
    trace.search_fixture(query)
    result = trace.verify("查询执行完毕。")
    trace.witness("exact-query", result)
    assert result.passed is expected, result


def test_duplicate_query_of_one_object_does_not_complete_second_object(tmp_path):
    trace = IndependentTrace(tmp_path, "查询《公司规定》；查询《公司规定的附录》。")
    trace.search_fixture("公司规定")
    trace.search_fixture("公司规定")
    assert not trace.verify("查询执行完毕。").passed


def test_old_run_query_evidence_cannot_satisfy_current_obligation(tmp_path):
    trace = IndependentTrace(tmp_path, "查询《公司规定》。")
    trace.search_fixture("公司规定")
    invocations, evidence, bindings = trace.state()
    old_calls = [replace(i, run_id="old-run") for i in invocations]
    old_evidence = [replace(e, run_id="old-run") for e in evidence]
    assert not trace.verify("查询执行完毕。", state=(old_calls, old_evidence, bindings)).passed


def test_knowledge_success_does_not_cover_missing_data_operation(tmp_path):
    trace = IndependentTrace(tmp_path, "删除值的空值；查询《公司规定》。")
    trace.search_fixture("公司规定")
    assert not trace.verify("操作结束。").passed


def test_aggregate_display_limit_keeps_full_total_and_exact_semantics(tmp_path):
    trace = RealTrace(tmp_path, "按频道汇总时长并给出总计。")
    parameters = {
        "group_col": "频道",
        "value_col": "时长",
        "agg": "sum",
        "sort": "group",
        "limit": 1,
    }
    assert trace.admit(parameters, tool="aggregate_preview") is None
    result = trace.execute(parameters, tool="aggregate_preview")
    assert result["rows"] == [
        {"group": "A", "value": 40.0, "count": 3, "row_count": 4, "valid_value_count": 3}
    ]
    assert result["truncated"] is True and result["group_total"] == 2
    assert result["total_value"] == 70.0
    assert trace.verify("时长总计70。").passed


@pytest.mark.parametrize(
    "extra",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.5},
        {"limit": "1"},
        {"sort": "descending"},
        {"sort": True},
        {"sort": "group", "limit": 1, "value_col": "备注"},
    ],
)
def test_aggregate_display_options_do_not_waive_schema_or_metric_identity(tmp_path, extra):
    trace = RealTrace(tmp_path, "按频道汇总时长并给出总计。")
    parameters = {"group_col": "频道", "value_col": "时长", "agg": "sum", **extra}
    assert trace.admit(parameters, tool="aggregate_preview") is not None


def test_sum_still_requires_explicit_metric_when_display_parameters_are_legal(tmp_path):
    trace = RealTrace(tmp_path, "按频道汇总时长并给出总计。")
    assert (
        trace.admit(
            {"group_col": "频道", "agg": "sum", "limit": 1, "sort": "group"},
            tool="aggregate_preview",
        )
        is not None
    )


def _default_group_count_contract_fixture(trace):
    """Bound non-null-group count fixture, never a row-count compiler PASS."""
    trace.contract = replace(
        trace.contract,
        analysis_requirements=replace(
            trace.contract.analysis_requirements,
            aggregation="count",
            value_col="频道",
            clarifications=(),
        ),
    )


def test_aggregate_default_group_count_can_omit_equivalent_optional_metric(tmp_path):
    trace = RealTrace(tmp_path, "按频道分组统计频道非空数。")
    _default_group_count_contract_fixture(trace)
    parameters = {"group_col": "频道", "agg": "count"}
    assert trace.admit(parameters, tool="aggregate_preview") is None
    result = trace.execute(parameters, tool="aggregate_preview")
    assert {row["group"]: row["value"] for row in result["rows"]} == {"A": 4, "B": 1}
    assert result["value_col"] is None
    assert trace.verify("分组统计已完成。").passed


def test_default_group_count_cannot_be_replaced_by_nullable_metric_count(tmp_path):
    trace = RealTrace(tmp_path, "按频道分组统计频道非空数。")
    _default_group_count_contract_fixture(trace)
    parameters = {"group_col": "频道", "value_col": "时长", "agg": "count", "limit": 1}
    assert trace.admit(parameters, tool="aggregate_preview") is not None
    result = trace.execute(parameters, tool="aggregate_preview")
    assert result["rows"][0]["value"] == 3  # Actual metric count differs from four A records.
    assert not trace.verify("分组统计已完成。").passed


def test_required_query_predecessor_blocks_data_operation_admission(tmp_path):
    trace = IndependentTrace(tmp_path, "先查询《policy_a》，再删除值的空值。")
    assert trace.admit({"drop_nulls": ["值"]}) is not None


def test_reversed_query_and_data_successes_cannot_complete(tmp_path):
    trace = IndependentTrace(tmp_path, "先查询《policy_a》，再删除值的空值。")
    trace.execute({"drop_nulls": ["值"]})  # Isolated deliberate admission bypass.
    assert len(load_dataframe(trace.current)) == 3
    trace.search_fixture("policy_a")
    result = trace.verify("本地处理结束。")
    trace.witness("reversed-query-data", result)
    assert not result.passed, result


def test_required_query_then_real_data_operation_completes(tmp_path):
    trace = IndependentTrace(tmp_path, "先查询《policy_a》，再删除值的空值。")
    trace.search_fixture("policy_a")
    assert trace.admit({"drop_nulls": ["值"]}) is None
    trace.execute({"drop_nulls": ["值"]})
    assert len(load_dataframe(trace.current)) == 3
    result = trace.verify("本地处理结束。")
    trace.witness("ordered-query-data", result)
    assert result.passed, result


def test_required_knowledge_successes_cannot_complete_in_reverse_source_order(tmp_path):
    trace = IndependentTrace(tmp_path, "先查询《policy_a》，再查询《policy_b》。")
    trace.search_fixture("policy_b")
    trace.search_fixture("policy_a")
    result = trace.verify("本地查询结束。")
    trace.witness("reversed-knowledge", result)
    assert not result.passed, result
