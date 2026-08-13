"""Stage 6B-2 deterministic data-role precondition tests."""

from __future__ import annotations

from apps.orchestrator.control.data_role_guard import (
    validate_data_role_preconditions,
    validate_role_requirements,
)
from packages.session.models import Dataset
from packages.session.task_models import DataRoleConfirmation


def _dataset() -> Dataset:
    return Dataset(
        ref="d" * 32,
        project_id="project",
        filename="anonymous.xlsx",
        profile={
            "row_count": 100,
            "column_count": 4,
            "columns": [
                {
                    "name": "发生时间",
                    "dtype": "datetime",
                    "null_ratio": 0.0,
                    "distinct_count": 100,
                },
                {
                    "name": "金额",
                    "dtype": "float",
                    "null_ratio": 0.0,
                    "distinct_count": 80,
                },
                {
                    "name": "客户",
                    "dtype": "str",
                    "null_ratio": 0.0,
                    "distinct_count": 100,
                },
                {
                    "name": "记录ID",
                    "dtype": "str",
                    "null_ratio": 0.0,
                    "distinct_count": 100,
                },
            ],
        },
        parent_ref=None,
        transform=None,
        created_at="2026-08-11T00:00:00Z",
    )


def _confirmation(*, data_hash: str = "a" * 64) -> DataRoleConfirmation:
    return DataRoleConfirmation(
        confirmation_id="c" * 32,
        run_id="run",
        question_id="group_column",
        dataset_ref="d" * 32,
        column="客户",
        role="dimension",
        plan_version=1,
        data_version_hash=data_hash,
        run_state_version=4,
        confirmed_at="2026-08-11T00:00:00Z",
    )


def test_high_confidence_trend_roles_pass_without_confirmation() -> None:
    result = validate_data_role_preconditions(
        tool_name="trend_analysis",
        arguments={
            "dataset_ref": "d" * 32,
            "time_col": "发生时间",
            "value_col": "金额",
        },
        dataset=_dataset(),
        confirmations=(),
        data_version_hash="a" * 64,
    )

    assert result is not None and result.allowed is True
    assert result.code == "data_role_preconditions_satisfied"
    assert [item["effective_role"] for item in result.checks] == ["time", "metric"]


def test_forecast_requires_time_and_metric_roles() -> None:
    result = validate_data_role_preconditions(
        tool_name="forecast",
        arguments={
            "dataset_ref": "d" * 32,
            "time_col": "发生时间",
            "value_col": "金额",
            "horizon": 4,
        },
        dataset=_dataset(),
        confirmations=(),
        data_version_hash="a" * 64,
    )

    assert result is not None and result.allowed is True
    assert [item["effective_role"] for item in result.checks] == ["time", "metric"]


def test_ambiguous_group_role_requires_current_data_version_confirmation() -> None:
    arguments = {
        "dataset_ref": "d" * 32,
        "group_col": "客户",
        "agg": "count",
    }
    blocked = validate_data_role_preconditions(
        tool_name="aggregate_preview",
        arguments=arguments,
        dataset=_dataset(),
        confirmations=(_confirmation(data_hash="b" * 64),),
        data_version_hash="a" * 64,
    )
    allowed = validate_data_role_preconditions(
        tool_name="aggregate_preview",
        arguments=arguments,
        dataset=_dataset(),
        confirmations=(_confirmation(),),
        data_version_hash="a" * 64,
    )

    assert blocked is not None and blocked.allowed is False
    assert blocked.code == "data_role_confirmation_required"
    assert allowed is not None and allowed.allowed is True
    assert allowed.checks[0]["source"] == "user_confirmation"
    assert allowed.checks[0]["confirmation_id"] == "c" * 32


def test_identifier_cannot_be_used_as_regression_metric() -> None:
    result = validate_data_role_preconditions(
        tool_name="regression",
        arguments={
            "dataset_ref": "d" * 32,
            "target": "金额",
            "features": ["记录ID"],
        },
        dataset=_dataset(),
        confirmations=(),
        data_version_hash="a" * 64,
    )

    assert result is not None and result.allowed is False
    assert result.code == "data_role_mismatch"
    assert result.checks[-1]["effective_role"] == "identifier"


def test_governed_group_tools_require_dimension_and_metric_roles() -> None:
    for tool_name, arguments in (
        (
            "dimension_contribution",
            {"dimension_col": "客户", "value_col": "金额"},
        ),
        ("group_compare", {"group_col": "客户", "value_col": "金额"}),
    ):
        result = validate_data_role_preconditions(
            tool_name=tool_name,
            arguments={"dataset_ref": "d" * 32, **arguments},
            dataset=_dataset(),
            confirmations=(_confirmation(),),
            data_version_hash="a" * 64,
        )

        assert result is not None and result.allowed is True
        assert [item["effective_role"] for item in result.checks] == [
            "dimension",
            "metric",
        ]


def test_incomplete_legacy_profile_fails_closed() -> None:
    dataset = _dataset()
    legacy = Dataset(
        ref=dataset.ref,
        project_id=dataset.project_id,
        filename=dataset.filename,
        profile={"row_count": 1, "column_count": 1, "columns": [{"name": "金额"}]},
        parent_ref=None,
        transform=None,
        created_at=dataset.created_at,
    )

    result = validate_data_role_preconditions(
        tool_name="anomaly_detect",
        arguments={"dataset_ref": dataset.ref, "value_col": "金额"},
        dataset=legacy,
        confirmations=(),
        data_version_hash="a" * 64,
    )

    assert result is not None and result.allowed is False
    assert result.code == "data_role_profile_unavailable"


def test_explicit_role_contract_is_reusable_for_future_join_keys() -> None:
    result = validate_role_requirements(
        requirements=(("left_key", "客户", frozenset({"dimension", "identifier"})),),
        dataset=_dataset(),
        confirmations=(_confirmation(),),
        data_version_hash="a" * 64,
    )

    assert result.allowed is True
    assert result.checks[0]["argument"] == "left_key"
    assert result.checks[0]["source"] == "user_confirmation"
