"""Independent normative integer examples; arithmetic oracle is literal."""

from apps.orchestrator.control.count_claims import parse_chinese_count

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

CASES = [
    ("十一", 11),
    ("一百一十", 110),
    ("一百一十一", 111),
    ("二百一十五", 215),
    ("一千零一十", 1010),
    ("一千零一十一", 1011),
    ("一千一百一十", 1110),
    ("一万零一十", 10010),
    ("九百九十九", 999),
]


@pytest.mark.parametrize("token,expected", CASES)
def test_normative_integer_place_values(token, expected):
    assert parse_chinese_count(token) == expected


@pytest.mark.parametrize("token,expected", CASES)
def test_supported_integer_fact_reaches_full_verifier(token, expected):
    args = {"dataset_ref": "a" * 32, "filters": [{"column": "频道", "op": "==", "value": "A"}]}
    result = {
        "dataset_ref": "b" * 32,
        "parent_ref": "a" * 32,
        "rows_before": expected + 1,
        "rows_after": expected,
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
    v, refs = verdict("筛选后共有" + token + "行。", [e], [iv])
    assert refs and v.passed, (refs, dataclasses.asdict(v))
    assert any(r.get("supported") and r.get("normalized") == str(expected) for r in refs), refs
