"""R2 frozen request oracle: exact literals/operations, never compiler-as-oracle."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import FrozenInstanceError

import pytest
from packages.orchestration import analysis_requirements as api

COLUMNS = ("月份", "频道", "状态", "播出时长（分钟）", "记录ID")
REF = "a" * 32


def compiled(text: str, *, roles: bool = True):
    bindings = ()
    if roles:
        bindings = tuple(
            api.ColumnRoleBinding(name, role)
            for name, role in zip(
                COLUMNS, ("time", "dimension", "dimension", "metric", "identifier"), strict=True
            )
        )
    return api.explicit_analysis_requirements(
        text, columns=COLUMNS, dataset_refs=(REF,), column_roles=bindings
    )


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("删除播出时长（分钟）为空的记录。", ["drop_nulls"]),
        ("按整行口径去重。", ["drop_duplicates"]),
        ("本轮只画像，暂不筛选或清洗。", []),
        ("不要去重，只说明去重建议。", []),
        ("先不筛选，改为筛选频道=A。", ["filter"]),
        ("请筛选频道=A，取消该筛选，只做画像。", []),
        ("先删除播出时长（分钟）的空值，再按整行去重。", ["drop_nulls", "drop_duplicates"]),
        ("先按整行去重，再删除播出时长（分钟）的空值。", ["drop_duplicates", "drop_nulls"]),
    ],
)
def test_current_operation_order_and_disposition(text, kinds):
    required = compiled(text)
    assert required.schema_version == 2
    assert [operation.kind for operation in required.active_operations()] == kinds
    assert not required.needs_clarification(), required.to_dict()
    assert not required.knowledge_queries


@pytest.mark.parametrize("connector", ["且", "并且", "，并且", " AND ", " && "])
def test_all_and_filters_are_bound(connector):
    required = compiled(f"请筛选频道=A{connector}状态=已播出。")
    assert list(required.filters) == [
        {"column": "频道", "op": "==", "value": "A"},
        {"column": "状态", "op": "==", "value": "已播出"},
    ]
    assert not required.needs_clarification()
    assert required.operation_arguments(required.active_operations()[0].operation_id) == {
        "filters": list(required.filters)
    }


@pytest.mark.parametrize(
    "text",
    [
        "筛选频道=A，并且状态已播出。",
        "筛选频道=A OR 状态=已播出。",
        '筛选状态="已播出"的记录。',
        "筛选频道=A且未提供列=已播出。",
    ],
)
def test_incomplete_or_ambiguous_filter_never_silently_executes_partial(text):
    required = compiled(text)
    assert required.needs_clarification()
    assert required.filter_error


def test_complete_quoted_literal_is_not_trimmed():
    required = compiled('筛选状态="已播出的记录"。')
    assert not required.needs_clarification()
    assert required.filters[0]["value"] == "已播出的记录"


@pytest.mark.parametrize(
    "text", ["按月份汇总播出时长（分钟）并计算总计。", "按月份汇总播出时长（分钟）的总计。"]
)
def test_month_column_group_metric_and_total_are_independent(text):
    required = compiled(text)
    assert (required.group_col, required.value_col, required.aggregation) == (
        "月份",
        "播出时长（分钟）",
        "sum",
    )
    assert required.total and required.total_column == required.value_col
    assert [item.kind for item in required.active_operations()] == ["aggregate"]
    assert not required.needs_clarification()


def test_total_alone_does_not_invent_group_obligation():
    required = compiled("请给出播出时长（分钟）的总计。")
    assert required.total and required.value_col == "播出时长（分钟）"
    assert required.aggregation == "sum" and required.group_col is None
    assert required.active_operations() == ()
    assert not required.needs_clarification()


def test_native_date_month_bucket_is_not_claimed_as_supported():
    required = compiled("请按月汇总播出时长（分钟）并给出总计。")
    assert required.time_grain == "month"
    assert any(item.reason == "unsupported_time_grain" for item in required.clarifications)


def test_role_candidates_do_not_turn_identifier_into_metric():
    required = compiled("按月份汇总记录ID并给出总计。")
    assert required.needs_clarification()
    assert required.value_col is None
    assert required.total_column is None


def test_no_role_information_requires_metric_resolution_and_preserves_other_slots():
    required = compiled("筛选频道=A。按月份汇总并给出总计。", roles=False)
    assert required.group_col == "月份" and required.value_col is None
    question = next(item for item in required.clarifications if item.slot == "value_col")
    assert question.reason == "metric_required"
    assert required.total and required.filters[0]["value"] == "A"


def test_source_roundtrip_and_defensive_projection_integrity():
    text = "  筛选频道=A。按月份汇总播出时长（分钟）的总计。  "
    required = compiled(text)
    assert required.source_hash == hashlib.sha256(text.strip().encode()).hexdigest()
    assert required.source_length == len(text.strip())
    required.validate_source(text.strip())
    assert api.AnalysisRequirements.from_dict(required.to_dict()) == required
    with pytest.raises(ValueError):
        required.validate_source(text.strip() + "改变")
    with pytest.raises(FrozenInstanceError):
        required.operations[0].kind = "drop_duplicates"
    required.filters[0]["value"] = "B"
    for entry in (required.validate_integrity, required.active_operations, required.to_dict):
        with pytest.raises(ValueError):
            entry()


@pytest.mark.parametrize(
    "mutation",
    [
        "alias",
        "span",
        "length",
        "revision",
        "operation_order",
        "operation_kind",
        "extra",
        "nonfinite",
    ],
)
def test_corrupt_persisted_v2_is_rejected(mutation):
    payload = copy.deepcopy(compiled("筛选频道=A。按月份汇总播出时长（分钟）的总计。").to_dict())
    if mutation == "alias":
        payload["filters"][0]["value"] = "B"
    elif mutation == "span":
        payload["operations"][0]["span"] = [0, 10000]
    elif mutation == "length":
        payload["source_length"] = True
    elif mutation == "revision":
        payload["revision"] = -1
    elif mutation == "operation_order":
        payload["operations"][0]["order"] = False
    elif mutation == "operation_kind":
        payload["operations"][0]["kind"] = "untrusted_execute"
    elif mutation == "extra":
        payload["trusted_by_model"] = True
    else:
        payload["operations"][0]["parameters"]["filters"][0]["value"] = float("nan")
    with pytest.raises(ValueError):
        api.AnalysisRequirements.from_dict(payload)


def test_legacy_constructor_and_record_are_not_implicitly_upgraded():
    required = api.AnalysisRequirements(total=True, total_column="cost")
    assert required.schema_version == 1
    payload = {
        "filters": [],
        "total": True,
        "total_column": "cost",
        "dataset_ref": None,
        "filter_error": None,
    }
    assert required.to_dict() == payload
    restored = api.AnalysisRequirements.from_dict(payload)
    assert restored.schema_version == 1 and restored.operations == ()


def test_resolve_exact_slot_keeps_order_filters_and_original_source():
    required = compiled("筛选频道=A。按月份汇总并计算总计。", roles=False)
    # Add a trusted role-backed candidate with a fresh compile, not a model label.
    roles = tuple(
        api.ColumnRoleBinding(name, role)
        for name, role in zip(
            COLUMNS, ("time", "dimension", "dimension", "metric", "metric"), strict=True
        )
    )
    required = api.explicit_analysis_requirements(
        required_goal := "筛选频道=A。按月份汇总并计算总计。",
        columns=COLUMNS,
        dataset_refs=(REF,),
        column_roles=roles,
    )
    resolved = api.resolve_analysis_slot(
        required, slot="value_col", answer="播出时长（分钟）", expected_revision=0, columns=COLUMNS
    )
    assert resolved.revision == 1 and resolved.source_hash == required.source_hash
    resolved.validate_source(required_goal)
    assert resolved.filters == required.filters
    assert [(item.operation_id, item.kind) for item in resolved.operations] == [
        (item.operation_id, item.kind) for item in required.operations
    ]
    assert resolved.total and resolved.value_col == resolved.total_column == "播出时长（分钟）"
    assert resolved.group_col == "月份" and not resolved.needs_clarification()
    with pytest.raises(ValueError):
        api.resolve_analysis_slot(required, slot="value_col", answer="状态", expected_revision=0)
    with pytest.raises(ValueError):
        api.resolve_analysis_slot(resolved, slot="value_col", answer="记录ID", expected_revision=0)


@pytest.mark.parametrize(
    "text", ["不要汇总或总计。只做画像。", "本轮暂不计算总计。", "请给出总计，取消该总计。"]
)
def test_total_disposition_does_not_become_an_extra_obligation(text):
    required = compiled(text)
    assert not required.total


@pytest.mark.parametrize("key", ["operations", "clarifications", "knowledge_queries"])
@pytest.mark.parametrize("bad", [None, True, "not-a-sequence"])
def test_malformed_collection_is_a_contract_error_not_a_crash(key, bad):
    payload = compiled("筛选频道=A。").to_dict()
    payload[key] = bad
    with pytest.raises(ValueError):
        api.AnalysisRequirements.from_dict(payload)


def test_separate_group_and_metric_clarifications_keep_every_pending_slot():
    bindings = tuple(
        api.ColumnRoleBinding(name, role)
        for name, role in zip(
            COLUMNS, ("time", "dimension", "dimension", "metric", "metric"), strict=True
        )
    )
    required = api.explicit_analysis_requirements(
        "筛选频道=A。分组汇总并给出总计。",
        columns=COLUMNS,
        dataset_refs=(REF,),
        column_roles=bindings,
    )
    assert [q.slot for q in required.clarifications] == ["group_col", "value_col"]
    group = api.resolve_analysis_slot(required, slot="group_col", answer="月份", columns=COLUMNS)
    assert [q.slot for q in group.clarifications] == ["value_col"]
    assert group.filters == required.filters and group.total
    with pytest.raises(ValueError):
        group.operation_arguments(group.active_operations()[-1].operation_id)


def test_and_filter_is_not_truncated_by_later_cleanup_operation():
    required = compiled("筛选频道=A，状态=已播出，再删除播出时长（分钟）的空值。")
    assert [operation.kind for operation in required.active_operations()] == [
        "filter",
        "drop_nulls",
    ]
    assert [condition["column"] for condition in required.filters] == ["频道", "状态"]
    assert not required.needs_clarification()


def test_repeated_cleanups_do_not_borrow_each_others_fields():
    required = compiled("先删除频道的空值，再删除状态的空值。")
    assert [operation.parameters.columns for operation in required.active_operations()] == [
        ("频道",),
        ("状态",),
    ]
    assert not required.needs_clarification()


def test_quoted_value_connector_is_data_and_unresolved_instruction_conflict_blocks():
    required = compiled('筛选状态="播出或录制"。')
    assert not required.needs_clarification()
    assert required.filters[0]["value"] == "播出或录制"
    conflict = compiled("筛选频道=A，不要筛选状态=已播出。")
    assert any(q.reason == "ambiguous_operation" for q in conflict.clarifications)


def test_generic_cleaning_ban_covers_both_supported_cleaning_actions_only():
    required = compiled("只做画像，暂不筛选或清洗。")
    assert {op.kind for op in required.operations if op.disposition == "prohibited"} == {
        "filter",
        "drop_nulls",
        "drop_duplicates",
    }
    narrow = compiled("不要去重。")
    assert {op.kind for op in narrow.operations if op.disposition == "prohibited"} == {
        "drop_duplicates"
    }


def test_filter_answer_reuses_complete_parser_and_preserves_other_operations():
    required = compiled("筛选频道=A，并且状态已播出。按月份汇总播出时长（分钟）的总计。")
    parsed = api.parse_filter_answer("筛选条件为频道=A且状态=已播出。", COLUMNS)
    assert [item["column"] for item in parsed] == ["频道", "状态"]
    resolved = api.resolve_analysis_slot(
        required,
        slot="filters",
        answer="筛选条件为频道=A且状态=已播出。",
        columns=COLUMNS,
    )
    assert resolved.filters == parsed and resolved.total and resolved.group_col == "月份"
    assert [op.kind for op in resolved.active_operations()] == ["filter", "aggregate"]
    assert not resolved.needs_clarification()
    cancelled = api.resolve_analysis_slot(
        required, slot="filters", answer="取消筛选", columns=COLUMNS
    )
    assert not cancelled.filters and cancelled.total and cancelled.group_col == "月份"
    assert [op.kind for op in cancelled.active_operations()] == ["aggregate"]


def test_unquoted_natural_suffix_keeps_literal_but_requires_actual_clarification():
    required = compiled("筛选频道=A且状态=已播出的记录，说明筛选前和筛选后的行数。")
    assert required.filters[-1]["value"] == "已播出的记录"
    assert required.needs_clarification()
    assert any(q.slot == "filters" for q in required.clarifications)


def test_boolean_and_numeric_conditions_are_not_deduplicated_by_python_equality():
    required = compiled("筛选频道=1且频道=true。")
    assert len(required.filters) == 2
    assert type(required.filters[0]["value"]) is int
    assert type(required.filters[1]["value"]) is bool


@pytest.mark.parametrize("column", ["播出时长（分钟）", "频道", "记录ID"])
def test_explicit_column_non_null_count_is_distinct_from_whole_row_count(column):
    required = compiled(f"按月份分组统计{column}的非空数。")
    assert not required.needs_clarification()
    assert required.aggregation == "count" and required.value_col == column
    assert required.operation_arguments(required.active_operations()[0].operation_id) == {
        "group_col": "月份",
        "value_col": column,
        "agg": "count",
    }


def test_non_null_count_missing_owner_is_unknown_and_described_count_does_not_leak():
    required = compiled("按月份分组统计非空数。")
    assert required.needs_clarification() and required.value_col is None
    described = compiled("只说明频道非空数口径；按月份汇总播出时长（分钟）。")
    assert not described.needs_clarification() and described.aggregation == "sum"


@pytest.mark.parametrize("method", ["最大值", "最小值", "中位数"])
def test_unsupported_method_is_not_silently_sum(method):
    required = compiled(f"按月份汇总播出时长（分钟），取{method}。")
    assert required.needs_clarification()
    assert required.aggregation is None


def test_definition_key_is_explicit_and_cannot_be_dropped_from_persisted_v2():
    goal = "按 metric.grouped_measure 汇总播出时长（分钟）。"
    required = compiled(goal)
    operation = required.active_operations()[0]
    assert operation.parameters.definition_key == "metric.grouped_measure"
    assert required.knowledge_queries[0].path == ("metric.grouped_measure",)
    left, right = required.knowledge_queries[0].source_spans[0]
    assert goal[left:right] == "metric.grouped_measure" and right < operation.span[0]
    restored = api.AnalysisRequirements.from_dict(required.to_dict())
    restored.validate_source(goal)
    assert restored == required
    malformed = required.to_dict()
    del malformed["operations"][0]["parameters"]["definition_key"]
    with pytest.raises(ValueError, match="操作参数形状"):
        api.AnalysisRequirements.from_dict(malformed)
    legacy = api.AnalysisRequirements.from_dict(
        {
            "filters": [],
            "total": False,
            "total_column": None,
            "dataset_ref": REF,
            "filter_error": None,
        }
    )
    assert legacy.schema_version == 1 and not legacy.operations


def test_definition_dependency_survives_only_the_selected_group_slot_resolution():
    goal = "按 metric.grouped_measure 汇总播出时长（分钟）。"
    required = compiled(goal)
    resolved = api.resolve_analysis_slot(required, slot="group_col", answer="月份", columns=COLUMNS)
    assert resolved.operations == required.operations
    assert resolved.knowledge_queries == required.knowledge_queries
    assert resolved.group_col == "月份" and resolved.revision == 1
    resolved.validate_source(goal)
