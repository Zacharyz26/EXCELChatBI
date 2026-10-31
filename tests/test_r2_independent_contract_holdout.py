"""Independent R2 compiler holdout; literal task contracts are the oracle."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from packages.orchestration.analysis_requirements import (
    AnalysisRequirements,
    ColumnRoleBinding,
    explicit_analysis_requirements,
    filter_arguments_match,
    resolve_analysis_slot,
)
from packages.orchestration.contracts import build_minimal_contract

COLUMNS = ("月份", "频道", "状态", "收入", "收入补充", "成本", "记录ID")
ROLES = tuple(
    ColumnRoleBinding(name, role)
    for name, role in zip(
        COLUMNS,
        ("time", "dimension", "dimension", "metric", "metric", "metric", "identifier"),
        strict=True,
    )
)
REF = "a" * 32


@pytest.mark.parametrize("column", ["组别", "不良率"])
@pytest.mark.parametrize("prohibited", [False, True])
def test_schema_field_words_do_not_override_explicit_operation_polarity(column, prohibited):
    text = ("不要" if prohibited else "") + "按" + column + "汇总收入。"
    request = explicit_analysis_requirements(
        text,
        columns=(column, "收入"),
        dataset_refs=(REF,),
        column_roles=(ColumnRoleBinding(column, "dimension"), ColumnRoleBinding("收入", "metric")),
    )
    if prohibited:
        assert not request.active_operations()
        assert any(
            o.kind == "aggregate" and o.disposition == "prohibited" for o in request.operations
        )
    else:
        assert not request.needs_clarification(), request.to_dict()
        assert [o.kind for o in request.active_operations()] == ["aggregate"]
        assert (request.group_col, request.value_col, request.aggregation) == (
            column,
            "收入",
            "sum",
        )


@pytest.mark.parametrize("noun", ["记录数量", "记录数"])
def test_record_count_does_not_infer_count_of_unique_nullable_metric(noun):
    request = explicit_analysis_requirements(
        "按频道分组统计" + noun + "。",
        columns=("频道", "收入"),
        dataset_refs=(REF,),
        column_roles=(ColumnRoleBinding("频道", "dimension"), ColumnRoleBinding("收入", "metric")),
    )
    # Existing aggregate count is COUNT(value_col), defaulting to the group
    # field. COUNT(*) cannot silently be replaced, especially for null groups.
    assert request.needs_clarification(), request.to_dict()
    assert [op.kind for op in request.active_operations()] == ["aggregate"]
    assert (request.group_col, request.value_col, request.aggregation) == ("频道", None, "count")
    with pytest.raises(ValueError):
        request.operation_arguments(request.active_operations()[0].operation_id)


@pytest.mark.parametrize("method", ["最大值", "最小值", "中位数"])
def test_explicit_unsupported_aggregate_method_never_defaults_to_sum(method):
    request = compile_goal("按月份汇总收入，取" + method + "。")
    assert request.needs_clarification(), request.to_dict()
    assert any(q.slot == "aggregation" for q in request.clarifications)
    with pytest.raises(ValueError):
        request.operation_arguments(request.active_operations()[0].operation_id)


@pytest.mark.parametrize("method", ["均值", "最小值", "中位数"])
def test_noncurrent_statistic_description_does_not_own_current_sum(method):
    request = compile_goal("只说明" + method + "口径；按月份汇总收入。")
    assert not request.needs_clarification(), request.to_dict()
    assert (request.group_col, request.value_col, request.aggregation) == ("月份", "收入", "sum")


@pytest.mark.parametrize("column", ["平均收入", "均值收入"])
def test_statistic_words_inside_metric_identity_are_not_aggregate_methods(column):
    request = explicit_analysis_requirements(
        "按频道汇总" + column + "。",
        columns=("频道", column),
        dataset_refs=(REF,),
        column_roles=(ColumnRoleBinding("频道", "dimension"), ColumnRoleBinding(column, "metric")),
    )
    assert not request.needs_clarification(), request.to_dict()
    assert (request.group_col, request.value_col, request.aggregation) == ("频道", column, "sum")


def test_explicit_mean_method_remains_distinct_from_mean_named_metric():
    request = explicit_analysis_requirements(
        "按频道分组计算平均收入的均值。",
        columns=("频道", "平均收入"),
        dataset_refs=(REF,),
        column_roles=(
            ColumnRoleBinding("频道", "dimension"),
            ColumnRoleBinding("平均收入", "metric"),
        ),
    )
    assert not request.needs_clarification(), request.to_dict()
    assert (request.group_col, request.value_col, request.aggregation) == (
        "频道",
        "平均收入",
        "mean",
    )


def compile_goal(goal):
    return explicit_analysis_requirements(
        goal, columns=COLUMNS, dataset_refs=(REF,), column_roles=ROLES
    )


@pytest.mark.parametrize("explanation", ["说明筛选前后记录数", "报告前后行数", "给出前后记录数"])
def test_explanation_tail_does_not_own_unparsed_and_operand(explanation):
    request = compile_goal("筛选频道=A，" + explanation + "，并且状态已播出。")
    assert request.needs_clarification(), request.to_dict()
    assert any(q.slot == "filters" for q in request.clarifications)
    with pytest.raises(ValueError):
        request.operation_arguments(request.active_operations()[0].operation_id)


@pytest.mark.parametrize("explanation", ["说明筛选前后记录数", "报告前后行数", "给出前后记录数"])
def test_complete_explanation_tail_retains_only_explicit_filter(explanation):
    request = compile_goal("筛选频道=A，" + explanation + "。")
    assert not request.needs_clarification(), request.to_dict()
    assert request.filters == ({"column": "频道", "op": "==", "value": "A"},)


@pytest.mark.parametrize("subject", ["收入补充", '"收入补充"', "收入补充和成本"])
def test_longer_field_identity_does_not_authorize_shorter_field(subject):
    request = compile_goal("删除" + subject + "的空值。")
    expected = ("收入补充", "成本") if subject.endswith("和成本") else ("收入补充",)
    assert not request.needs_clarification(), request.to_dict()
    assert request.operation_arguments(request.active_operations()[0].operation_id) == {
        "drop_nulls": list(expected)
    }


def test_named_withdrawal_does_not_cancel_other_same_family_object():
    request = compile_goal("删除收入的空值；删除成本的空值；撤回删除收入的空值。")
    # A finite implementation may ask if object binding is unresolved, but must
    # not silently complete after canceling the independent cost obligation.
    if request.needs_clarification():
        assert any(q.slot in {"operation", "columns"} for q in request.clarifications)
    else:
        assert [op.parameters.columns for op in request.active_operations()] == [("成本",)]


def test_revision_of_cost_does_not_remove_income_prohibition():
    request = compile_goal("不要删除收入的空值；现在执行删除成本的空值。")
    assert request.needs_clarification() or any(
        op.kind == "drop_nulls" and op.disposition == "prohibited" for op in request.operations
    ), request.to_dict()


@pytest.mark.parametrize(
    "text", ["按月份求和收入；按月份平均收入。", "按月份平均收入，然后按月份求和收入。"]
)
def test_distinct_aggregate_requirements_cannot_collapse_to_one_method(text):
    request = compile_goal(text)
    # Frozen R2 schema owns one aggregate slot. Conflicting required methods
    # cannot be represented by whichever word a global regex happens to find.
    assert request.needs_clarification(), request.to_dict()


def test_same_aggregate_grouping_phrase_is_not_multiple_business_requests():
    request = compile_goal("按月份分组汇总收入。")
    assert not request.needs_clarification(), request.to_dict()
    assert [op.kind for op in request.active_operations()] == ["aggregate"]
    assert (request.group_col, request.value_col, request.aggregation) == ("月份", "收入", "sum")


@pytest.mark.parametrize("literal", ["已播出；待复核", "已播出。待复核", "已播出,待复核"])
def test_complete_quoted_filter_literal_owns_its_internal_punctuation(literal):
    request = compile_goal('筛选状态="' + literal + '"。')
    assert not request.needs_clarification(), request.to_dict()
    assert request.filters == ({"column": "状态", "op": "==", "value": literal},)


@pytest.mark.parametrize("version", [None, 1])
def test_v2_payload_cannot_be_downgraded_by_dropping_version_only(version):
    payload = compile_goal("筛选频道=A。").to_dict()
    if version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = version
    with pytest.raises(ValueError):
        AnalysisRequirements.from_dict(payload)


@pytest.mark.parametrize(
    "corruption", ["empty_path", "named_all_sources", "empty_sources", "mutable_span"]
)
def test_knowledge_scope_serialization_keeps_literal_identity_and_source_contract(corruption):
    request = compile_goal("查询《公司规定》。")
    if corruption == "mutable_span":
        query = request.knowledge_queries[0]
        malformed = replace(query, source_spans=([0, 2],))
        with pytest.raises(ValueError):
            replace(request, knowledge_queries=(malformed,)).validate_integrity()
        return
    payload = request.to_dict()
    query = payload["knowledge_queries"][0]
    if corruption == "empty_path":
        query["path"] = []
    elif corruption == "named_all_sources":
        query["all_sources"] = True
    else:
        query["source_spans"] = []
    with pytest.raises(ValueError):
        AnalysisRequirements.from_dict(payload)


def test_unbound_total_cannot_lose_its_pending_metric_question():
    request = compile_goal("给出总计。")
    assert request.value_col is None and request.clarifications
    payload = request.to_dict()
    payload["clarifications"] = []
    with pytest.raises(ValueError):
        AnalysisRequirements.from_dict(payload)


def test_group_answer_preserves_pending_metric_and_every_filter():
    request = compile_goal("筛选频道=A，状态=已播出；分组汇总并给出总计。")
    assert {q.slot for q in request.clarifications} == {"group_col", "value_col"}
    restored = AnalysisRequirements.from_dict(copy.deepcopy(request.to_dict()))
    grouped = resolve_analysis_slot(
        restored, slot="group_col", answer="月份", expected_revision=0, columns=COLUMNS
    )
    assert grouped.group_col == "月份" and grouped.value_col is None
    assert grouped.total and grouped.total_column is None
    assert grouped.filters == request.filters and grouped.operations == request.operations
    assert grouped.source_hash == request.source_hash and grouped.dataset_ref == REF
    assert [q.slot for q in grouped.clarifications] == ["value_col"]
    with pytest.raises(ValueError):
        grouped.operation_arguments(grouped.active_operations()[-1].operation_id)
    final = resolve_analysis_slot(
        grouped, slot="value_col", answer="收入", expected_revision=1, columns=COLUMNS
    )
    assert final.total_column == final.value_col == "收入"
    assert final.filters == request.filters and final.group_col == "月份"
    assert final.revision == 2 and not final.needs_clarification()


@pytest.mark.parametrize("answer", ["收入", "记录ID", "不存在字段"])
def test_group_answer_cannot_promote_metric_or_identifier_to_group(answer):
    request = compile_goal("分组汇总收入。")
    with pytest.raises(ValueError):
        resolve_analysis_slot(
            request, slot="group_col", answer=answer, expected_revision=0, columns=COLUMNS
        )


def test_task_contract_hash_cannot_hide_changed_source_text():
    contract = build_minimal_contract(
        run_id="independent",
        user_text="筛选频道=A。",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        columns=COLUMNS,
        dataset_refs=(REF,),
        column_roles=ROLES,
    )
    with pytest.raises(ValueError):
        replace(contract, goal="筛选频道=B。").to_dict()


def test_boolean_is_not_numeric_filter_operand_identity():
    request = compile_goal("筛选收入=1。")
    assert filter_arguments_match(
        request, {"dataset_ref": REF, "filters": [{"column": "收入", "op": "==", "value": 1}]}
    )
    assert not filter_arguments_match(
        request, {"dataset_ref": REF, "filters": [{"column": "收入", "op": "==", "value": True}]}
    )


def test_described_statistic_cannot_supply_current_aggregate_method():
    request = compile_goal("只说明平均值口径；按月份汇总收入。")
    assert not request.needs_clarification(), request.to_dict()
    assert request.aggregation == "sum"
    operation = request.active_operations()[0]
    assert request.operation_arguments(operation.operation_id) == {
        "group_col": "月份",
        "value_col": "收入",
        "agg": "sum",
    }
