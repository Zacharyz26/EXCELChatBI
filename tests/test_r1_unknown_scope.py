"""Explicit extra scope is an obligation even when its field is absent."""

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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("客户=VIP筛选后38行。", False),
        ("频道=A且客户=VIP筛选后38行。", False),
        ("状态=完成筛选后38行。", False),
        ("频道=A筛选后38行。", True),
    ],
)
def test_explicit_unapplied_filter_is_not_ignored(text, expected):
    args = {"dataset_ref": "a" * 32, "filters": [{"column": "频道", "op": "==", "value": "A"}]}
    result = {
        "dataset_ref": "b" * 32,
        "parent_ref": "a" * 32,
        "rows_before": 75,
        "rows_after": 38,
        "columns": ["频道", "状态"],
        "transform": {"filters": args["filters"]},
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
    e = EvidenceRecord(
        "ev", "run", "iv", None, "tool_result", {"tool": "transform_dataset"}, h, s, "now"
    )
    iv = ToolInvocation(
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
    )
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))
