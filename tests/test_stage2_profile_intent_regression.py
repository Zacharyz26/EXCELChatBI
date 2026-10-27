"""阶段 2 真实画像阻塞的不可变离线回归；不得访问网络或真实账本。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.task_outline import _requested_capabilities
from packages.session.task_models import EvidenceRecord

FIXTURE = Path(__file__).parent / "fixtures" / "stage2_profile_blocker.json"

# 每个原始数字都保留；拒绝项不能因同值或宽松文本匹配被放行。
EXPECTED_26 = [
    ("75", "$.profile.row_count", "dataset_shape"),
    ("5", "$.profile.column_count", "dataset_shape"),
    ("6", "$.profile.columns[0].distinct_count", "role_aside_distinct"),
    ("3", "$.profile.columns[1].distinct_count", "role_aside_distinct"),
    ("2", "$.profile.columns[2].distinct_count", "role_aside_distinct"),
    ("74", "$.profile.columns[4].distinct_count", "role_aside_distinct"),
    ("75", "$.profile.row_count", "declared_population_size"),
    ("10.0", "$.profile.columns[3].min", "profile_statistic"),
    ("999.0", "$.profile.columns[3].max", "profile_statistic"),
    ("50.121622", "$.profile.columns[3].mean", "profile_statistic"),
    ("37.5", "$.profile.columns[3].median", "profile_statistic"),
    ("113.152819", "$.profile.columns[3].std", "profile_statistic"),
    ("37", "$.profile.columns[3].distinct_count", "profile_statistic"),
    ("1", "$.profile.columns[3].null_count", "continued_profile_statistic"),
    (
        "74",
        "$.roles.columns[3].profile_evidence.non_null_count",
        "continued_profile_statistic",
    ),
    ("0.0133", "$.profile.columns[3].null_ratio", "continued_profile_statistic"),
    ("1.33%", "$.profile.columns[3].null_ratio", "ratio_restatement"),
    ("1", None, "ambiguous_duplicate_count_basis"),
    ("0.0133", None, "ambiguous_duplicate_clause"),
    ("1.33%", None, "ambiguous_duplicate_clause"),
    ("1", None, "ambiguous_duplicate_record_count"),
    ("74", "$.profile.columns[4].distinct_count", "explicit_field_distinct"),
    ("75", "$.profile.row_count", "dataset_shape"),
    ("1", None, "ambiguous_duplicate_value_basis"),
    ("1", None, "unqualified_field_alias"),
    ("0", None, "negated_advice_not_fact"),
]


def _load() -> tuple[dict, EvidenceRecord]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = fixture["evidence"]
    evidence = EvidenceRecord(
        raw["evidence_id"],
        raw["run_id"],
        raw["invocation_id"],
        raw["artifact_id"],
        raw["kind"],
        raw["source"],
        raw["result_hash"],
        raw["summary"],
        raw["created_at"],
    )
    return fixture, evidence


def _refs(text: str, goal: str, evidence: EvidenceRecord) -> list[dict]:
    claims = extract_numeric_claims(final_text=text, goal=goal, evidence=[evidence])
    return [ref for claim in claims for ref in claim.value_refs]


def test_fixture_preserves_exact_real_request_response_evidence_and_plan() -> None:
    fixture, evidence = _load()
    assert fixture["schema"] == "stage2-profile-blocker-v1"
    assert "暂不筛选或清洗" in fixture["goal"]
    assert len(fixture["candidate"]) == 523
    assert hashlib.sha256(fixture["candidate"].encode()).hexdigest() == (
        "a7bec8195810a13705cc4d64e849f18186d2c378dc0dcabe44cbc089d35de2c1"
    )
    assert evidence.evidence_id == "eaa8adabee2a4a47920ad81a081a733b"
    context = evidence.summary["numeric_context"]
    assert context["dataset_ref"] == "97714653b62d4ae194f013f46b9f2077"
    assert context["data_version_hash"] == (
        "ab7421b144cae2ca4c09cef3d80eacde2a7d48611148ac16257d612c30be2349"
    )
    assert context["profile_population"]["steps"] == []
    assert fixture["raw_capture"]["002-original-data.jsonl"] == {
        "sha256": "cd4a3f87fcc105042f911436bc1beac01df8c8cebe9b0c8f6ef355235b15b77b",
        "chunks": 349,
        "finish_reasons": ["stop"],
        "terminal_usage": [
            {
                "prompt_tokens": 7787,
                "completion_tokens": 347,
                "total_tokens": 8134,
                "prompt_tokens_details": {"cached_tokens": 896},
                "prompt_cache_hit_tokens": 896,
                "prompt_cache_miss_tokens": 6891,
            }
        ],
        "content_sha256": "a7bec8195810a13705cc4d64e849f18186d2c378dc0dcabe44cbc089d35de2c1",
        "content_chars": 523,
        "exact_equal_to_safe_final_candidate": True,
    }
    assert [step["step"]["capability"] for step in fixture["plan_steps"]] == [
        "data.profile",
        "dataset.transform",
    ]
    assert [step["status"] for step in fixture["plan_steps"]] == ["completed", "pending"]


def test_exact_real_candidate_classifies_all_26_refs_without_widening() -> None:
    fixture, evidence = _load()
    refs = _refs(fixture["candidate"], fixture["goal"], evidence)
    actual = [(ref["token"], ref.get("path") if ref.get("supported") else None) for ref in refs]
    assert actual == [(token, path) for token, path, _ in EXPECTED_26]
    assert sum(ref.get("supported") is True for ref in refs) == 19
    assert sum(ref.get("supported") is not True for ref in refs) == 7


def test_exact_goal_no_longer_plans_forbidden_transform() -> None:
    fixture, _ = _load()
    assert fixture["plan_steps"][1]["status"] == "pending"  # 原始错误计划原样留存
    assert _requested_capabilities(fixture["goal"], {}) == ["data.profile"]


def test_correct_values_bind_and_wrong_value_still_rejects() -> None:
    fixture, evidence = _load()
    correct = _refs("播出时长（分钟）最大值999.0。", fixture["goal"], evidence)
    wrong = _refs("播出时长（分钟）最大值998.0。", fixture["goal"], evidence)
    shape = _refs("数据规模为75行、5列。", fixture["goal"], evidence)
    assert len(correct) == 1 and correct[0]["supported"] is True
    assert len(wrong) == 1 and wrong[0]["supported"] is False
    assert len(shape) == 2 and all(ref["supported"] for ref in shape)
