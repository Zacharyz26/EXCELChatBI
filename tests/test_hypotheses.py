"""Stage 6C-1 bounded candidate-hypothesis screening tests."""

from __future__ import annotations

from apps.orchestrator.control.hypotheses import screen_candidate_hypotheses
from packages.session.models import Dataset


def _dataset(*, ref: str = "d" * 32) -> Dataset:
    return Dataset(
        ref=ref,
        project_id="project",
        filename="sales.xlsx",
        profile={
            "row_count": 100,
            "column_count": 5,
            "columns": [
                {"name": "日期", "dtype": "datetime", "null_ratio": 0.0, "distinct_count": 100},
                {"name": "销售额", "dtype": "float", "null_ratio": 0.0, "distinct_count": 80},
                {"name": "利润", "dtype": "float", "null_ratio": 0.0, "distinct_count": 60},
                {"name": "地区", "dtype": "str", "null_ratio": 0.0, "distinct_count": 5},
                {"name": "订单ID", "dtype": "str", "null_ratio": 0.0, "distinct_count": 100},
            ],
        },
        parent_ref=None,
        transform=None,
        created_at="2026-08-11T00:00:00Z",
    )


def _catalog(
    *, correlation: bool = True, group_compare: bool | None = None
) -> list[dict[str, object]]:
    catalog: list[dict[str, object]] = [
        {"name": "stats.trend", "allowed": True},
        {"name": "stats.anomaly", "allowed": True},
        {"name": "data.aggregate", "allowed": True},
        {"name": "stats.correlation", "allowed": correlation},
    ]
    if group_compare is not None:
        catalog.append({"name": "stats.group_compare", "allowed": group_compare})
    return catalog


def test_open_exploration_produces_bounded_deterministic_untested_candidates() -> None:
    first = screen_candidate_hypotheses(
        user_text="请深入分析这份数据",
        datasets=[_dataset()],
        capability_catalog=_catalog(),
        data_version_hash="a" * 64,
    )
    second = screen_candidate_hypotheses(
        user_text="请深入分析这份数据",
        datasets=[_dataset()],
        capability_catalog=_catalog(),
        data_version_hash="a" * 64,
    )

    assert first == second
    assert first is not None
    assert first["schema"] == "chatbi-hypothesis-screening-v1"
    assert first["raw_rows_read"] is False
    assert first["blocking_reason"] == "user_selection_required"
    candidates = first["candidates"]
    assert len(candidates) == 4
    assert all(item["status"] == "eligible" for item in candidates)
    assert all(item["tested"] is False for item in candidates)
    assert len(first["eligible_candidate_ids"]) == 4


def test_segment_candidate_upgrades_to_governed_group_compare_when_available() -> None:
    result = screen_candidate_hypotheses(
        user_text="请深入分析这份数据",
        datasets=[_dataset()],
        capability_catalog=_catalog(group_compare=True),
        data_version_hash="a" * 64,
    )

    assert result is not None
    segment = next(
        item for item in result["candidates"] if item["kind"] == "segment_comparison"
    )
    assert segment["capability"] == "stats.group_compare"
    assert "Welch" in segment["expected_evidence"]


def test_declared_but_unavailable_group_compare_does_not_fallback_to_aggregate() -> None:
    catalog = _catalog(group_compare=False)
    result = screen_candidate_hypotheses(
        user_text="请深入分析这份数据",
        datasets=[_dataset()],
        capability_catalog=catalog,
        data_version_hash="a" * 64,
    )

    assert result is not None
    segment = next(
        item for item in result["candidates"] if item["kind"] == "segment_comparison"
    )
    assert segment["capability"] == "stats.group_compare"
    assert segment["status"] == "rejected"
    assert "capability_unavailable" in segment["reason_codes"]


def test_capability_and_role_screening_fail_closed() -> None:
    dataset = _dataset()
    profile = dict(dataset.profile)
    profile["columns"] = list(profile["columns"][:2])
    profile["column_count"] = 2
    limited = Dataset(
        ref=dataset.ref,
        project_id=dataset.project_id,
        filename=dataset.filename,
        profile=profile,
        parent_ref=None,
        transform=None,
        created_at=dataset.created_at,
    )

    result = screen_candidate_hypotheses(
        user_text="分析数据",
        datasets=[limited],
        capability_catalog=_catalog(correlation=False),
        data_version_hash="b" * 64,
    )

    assert result is not None
    by_kind = {item["kind"]: item for item in result["candidates"]}
    assert by_kind["trend"]["status"] == "eligible"
    assert by_kind["anomaly"]["status"] == "eligible"
    assert by_kind["segment_comparison"]["status"] == "rejected"
    assert "dimension_role_unavailable" in by_kind["segment_comparison"]["reason_codes"]
    assert by_kind["correlation"]["status"] == "rejected"
    assert "capability_unavailable" in by_kind["correlation"]["reason_codes"]


def test_multiple_datasets_require_explicit_selection_before_screening() -> None:
    other = _dataset(ref="e" * 32)
    blocked = screen_candidate_hypotheses(
        user_text="分析这份数据",
        datasets=[_dataset(), other],
        capability_catalog=_catalog(),
        data_version_hash="c" * 64,
    )
    selected = screen_candidate_hypotheses(
        user_text="分析这份数据",
        datasets=[_dataset(), other],
        capability_catalog=_catalog(),
        data_version_hash="c" * 64,
        verified_dataset_refs=frozenset({other.ref}),
    )

    assert blocked is not None
    assert blocked["blocking_reason"] == "dataset_selection_required"
    assert blocked["candidates"] == []
    assert selected is not None
    assert selected["dataset_ref"] == other.ref
    assert len(selected["candidates"]) == 4


def test_specific_analysis_request_does_not_create_parallel_hypothesis_state() -> None:
    assert screen_candidate_hypotheses(
        user_text="分析销售额趋势",
        datasets=[_dataset()],
        capability_catalog=_catalog(),
        data_version_hash="d" * 64,
    ) is None
