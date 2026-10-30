"""Independent stage identity and operation population holdouts, fixed pre-fix."""

from apps.orchestrator.control.claims import _semantic_binding

from tests.test_r1_post_review_contracts import (
    EvidenceRecord,
    ToolInvocation,
    build_evidence_summary,
    dataclasses,
    hashlib,
    json,
    pytest,
    seal_count_facts,
    verdict,
)
from tests.test_r1_post_review_contracts import offline_review as offline_review


def evidence_for(operations, before, after, steps):
    args = {"dataset_ref": "a" * 32, **operations}
    result = {
        "dataset_ref": "b" * 32,
        "parent_ref": "a" * 32,
        "rows_before": before,
        "rows_after": after,
        "transform": operations,
        "row_changes": {"schema_version": 1, "removed_count": before - after, "steps": steps},
    }
    h = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    s = build_evidence_summary(
        summary="",
        result=result,
        artifact_id=None,
        arguments={**args, "data_version_hash": "branch1"},
    )
    s = seal_count_facts(
        summary=s,
        result=result,
        tool="transform_dataset",
        arguments=args,
        run_id="run",
        invocation_id="iv",
        result_hash=h,
        branch_hash="branch1",
    )
    return (
        EvidenceRecord(
            "ev", "run", "iv", None, "tool_result", {"tool": "transform_dataset"}, h, s, "now"
        ),
        ToolInvocation(
            "iv",
            "run",
            None,
            "call",
            "transform_dataset",
            "key",
            "args",
            args,
            "succeeded",
            h,
            None,
            None,
            "now",
            "now",
        ),
    )


def multi():
    return evidence_for(
        {"filters": [{"column": "频道", "op": "==", "value": "A"}], "drop_duplicates": []},
        75,
        37,
        [
            {
                "operation": "filters",
                "rows_before": 75,
                "rows_after": 38,
                "removed_count": 37,
                "count_basis": "removed_rows",
                "columns": None,
            },
            {
                "operation": "drop_duplicates",
                "rows_before": 38,
                "rows_after": 37,
                "removed_count": 1,
                "count_basis": "duplicate_excess",
                "columns": [],
            },
        ],
    )


@pytest.mark.parametrize(
    "path,wrong_ref",
    [
        ("$.row_changes.steps[0].rows_after", "b" * 32),
        ("$.row_changes.steps[1].rows_before", "a" * 32),
    ],
)
def test_intermediate_state_never_claims_endpoint_dataset(path, wrong_ref):
    e, _ = multi()
    binding = _semantic_binding(e, path)
    assert binding["dataset_ref"] != wrong_ref, binding
    assert binding.get("statistic") in {"before", "after"}, binding


@pytest.mark.parametrize(
    "text,expected",
    [
        ("筛选后38行。", True),
        ("数据集=" + "a" * 32 + "，去重前38行。", False),
        ("数据集=" + "b" * 32 + "，去重后37行。", True),
    ],
)
def test_multistep_identity(text, expected):
    e, iv = multi()
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("按频道去重删除73行。", True),
        ("整行去重删除73行。", False),
        ("按状态去重删除73行。", False),
        ("去重后2行。", True),
    ],
)
def test_subset_dedup_is_not_whole_row_or_other_column(text, expected):
    e, iv = evidence_for(
        {"drop_duplicates": ["频道"]},
        75,
        2,
        [
            {
                "operation": "drop_duplicates",
                "rows_before": 75,
                "rows_after": 2,
                "removed_count": 73,
                "count_basis": "duplicate_excess",
                "columns": ["频道"],
            }
        ],
    )
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))


@pytest.mark.parametrize(
    "text,expected", [("按播出时长（分钟）清洗后37行。", True), ("按状态清洗后37行。", False)]
)
def test_null_cleanup_column_scope(text, expected):
    e, iv = evidence_for(
        {"drop_nulls": ["播出时长（分钟）"]},
        38,
        37,
        [
            {
                "operation": "drop_nulls",
                "rows_before": 38,
                "rows_after": 37,
                "removed_count": 1,
                "count_basis": "removed_rows",
                "columns": None,
            }
        ],
    )
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))
