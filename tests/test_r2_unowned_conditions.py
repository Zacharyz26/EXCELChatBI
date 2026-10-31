"""Independent unowned-condition safety oracle; actual tools and isolated SQLite."""

from __future__ import annotations

import pytest
from packages.common.dataset_store import load_dataframe

from tests.test_r2_operation_completion import no_network  # noqa: F401
from tests.test_r2_scope_holdout import IndependentTrace

FIRST = {"column": "组别", "op": "==", "value": "A"}
SECOND = {"column": "值", "op": ">", "value": 1}
COMPLETE = {"filters": [FIRST, SECOND]}
PARTIAL = {"filters": [FIRST]}


@pytest.mark.parametrize("separator", ["\n", "；"])
@pytest.mark.parametrize("comparison", ["值>1", "值大于1"])
def test_bare_following_condition_requires_complete_binding_or_clarification(
    tmp_path, separator, comparison
):
    trace = IndependentTrace(tmp_path, "筛选组别=A" + separator + comparison + "。")
    required = trace.contract.analysis_requirements
    # Literal four-row oracle: both A rows have value1, so the complete AND is
    # empty; applying only A leaves two rows and changes the business scope.
    rows = [("A", 1), ("A", 1), ("B", None), ("B", 2)]
    assert len([r for r in rows if r[0] == "A" and r[1] is not None and r[1] > 1]) == 0
    if not required.needs_clarification():
        operations = required.active_operations()
        assert len(operations) == 1
        assert required.operation_arguments(operations[0].operation_id) == COMPLETE
        assert trace.admit(COMPLETE) is None
    assert trace.admit(PARTIAL) is not None


@pytest.mark.parametrize("separator", ["\n", "；"])
@pytest.mark.parametrize("comparison", ["值>1", "值大于1"])
def test_partial_success_cannot_finish_a_request_with_unowned_condition(
    tmp_path, separator, comparison
):
    trace = IndependentTrace(tmp_path, "筛选组别=A" + separator + comparison + "。")
    trace.execute(PARTIAL)  # Deliberate isolated admission bypass for full Verifier.
    assert len(load_dataframe(trace.current)) == 2
    result = trace.verify("处理结束。")
    trace.witness("partial-unowned-condition", result)
    assert not result.passed, result


def test_explicit_complete_and_keeps_scope_and_tool_rejects_empty_result(tmp_path):
    trace = IndependentTrace(tmp_path, "筛选组别=A AND 值>1。")
    assert trace.admit(COMPLETE) is None
    with pytest.raises(ValueError, match="变换后数据集为空"):
        trace.execute(COMPLETE)
    assert trace.current == trace.source
    assert len(load_dataframe(trace.current)) == 4
    assert not trace.verify("处理结束。").passed


def test_explicit_nonempty_and_produces_only_matching_business_row(tmp_path):
    trace = IndependentTrace(tmp_path, "筛选组别=B AND 值>1。")
    parameters = {"filters": [{"column": "组别", "op": "==", "value": "B"}, SECOND]}
    assert trace.admit(parameters) is None
    trace.execute(parameters)
    frame = load_dataframe(trace.current)
    assert list(zip(frame["组别"], frame["值"], strict=True)) == [("B", 2.0)]
    assert trace.verify("处理结束。").passed


def test_two_explicit_filter_operations_keep_separate_authorized_steps(tmp_path):
    trace = IndependentTrace(tmp_path, "筛选组别=A；然后筛选值>1。")
    required = trace.contract.analysis_requirements
    assert not required.needs_clarification()
    operations = required.active_operations()
    assert [op.kind for op in operations] == ["filter", "filter"]
    assert required.operation_arguments(operations[0].operation_id) == PARTIAL
    assert required.operation_arguments(operations[1].operation_id) == {"filters": [SECOND]}
    assert trace.admit(PARTIAL) is None
    trace.execute(PARTIAL)
    assert len(load_dataframe(trace.current)) == 2
    assert trace.admit({"filters": [SECOND]}) is None
    with pytest.raises(ValueError, match="变换后数据集为空"):
        trace.execute({"filters": [SECOND]})
    assert len(load_dataframe(trace.current)) == 2
    assert not trace.verify("处理结束。").passed


@pytest.mark.parametrize(
    "explanation",
    [
        "这里只说明表达式“值>1”的含义，暂不应用它。",
        "以下只是示例：\n值>1。\n以上不是本次请求。",
    ],
)
def test_noncurrent_quoted_or_paired_example_does_not_add_an_executable_condition(
    tmp_path, explanation
):
    trace = IndependentTrace(tmp_path, "筛选组别=A。\n" + explanation)
    required = trace.contract.analysis_requirements
    assert not required.needs_clarification(), required.to_dict()
    operations = required.active_operations()
    assert len(operations) == 1
    assert required.operation_arguments(operations[0].operation_id) == PARTIAL
    assert trace.admit(PARTIAL) is None
    trace.execute(PARTIAL)
    assert len(load_dataframe(trace.current)) == 2
    assert trace.verify("处理结束。").passed
