"""Frozen R1 core cases: source quantity ownership and complete delivery.

The oracle uses local dataframe/profile producers and explicit independent
counts. No model call or verifier output is used to derive the expectation.
"""

import json
import socket
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import prepare_candidate, validate_candidate
from mcp_servers.excel_parser.advisor import diagnose_data_quality, infer_data_roles
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe

from tests import test_delimited_numeric_values as checks
from tests import test_profile_subject_boundaries as original
from tests.test_profile_claim_binding import CAPTURE, EXPLICIT_ANSWER


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **kw):
        raise AssertionError("R1 core quantity tests forbid network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.fixture(scope="module")
def scalar_profile():
    return original.actual_profile.__wrapped__()


CASES = [
    ("unknown_multiline", "收入（元）有（\nb17\n）个\n「缺失值」。", False),
    ("unknown_single", "收入（元）有（b17）个「缺失值」。", False),
    ("supported_multiline", "收入（元）有（\n1\n）个\n「缺失值」。", True),
    ("wrong_multiline", "收入（元）有（\n3\n）个\n「缺失值」。", False),
    ("unknown_date", "收入（元）有（\n2026-10-16\n）个\n「缺失值」。", False),
    ("deictic_point", "画像也印证了这一点——收入（元）空值率25%。", True),
    ("deictic_wrong_count", "画像也印证了这一点——收入（元）有8个缺失值。", False),
    ("another_label", "这里是两个标签：“甲”“乙”，另一名称是“旁注”。", True),
    ("another_person", "本表还有另一名客户。", False),
    ("another_entity", "本表还有另一个体。", False),
]


@pytest.mark.parametrize("name,text,expected", CASES, ids=[c[0] for c in CASES])
def test_core_count_ownership(scalar_profile, name, text, expected, tmp_path):
    result = checks.evaluate(scalar_profile, text)
    evidence = [original.record(scalar_profile)]
    candidate = validate_candidate(text, evidence, goal="复核画像")
    prepared = prepare_candidate(text, evidence, goal="复核画像")
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {"text": text, "expected": expected, "result": result, "candidate": asdict(candidate)},
            ensure_ascii=False,
            indent=2,
        )
    )
    assert result["verification"]["passed"] is expected, result
    assert (result["report"]["status"] == "passed") is expected, result
    assert candidate.passed is expected, asdict(candidate)
    assert prepared.valid is expected, asdict(prepared)
    if name.startswith("unknown_"):
        assert candidate.quantities, "Unknown numeric assignment cannot disappear"
        assert any(q["obligation"] == "blocked" for q in candidate.quantities)
    if name == "supported_multiline":
        refs = result["refs"]
        assert any(r["token"] == "1" and r.get("supported") is True for r in refs)
    if name.startswith("deictic_"):
        assert all(q["token"] != "一" for q in candidate.quantities)


@pytest.fixture(scope="module")
def broadcast_profile():
    data = pd.read_csv(Path(__file__).parents[1] / "examples/handoff/broadcast.csv")
    assert len(data) == 75 and data["播出时长（分钟）"].isna().sum() == 1
    profile = infer_schema({"dataset_ref": save_dataframe(data)})
    roles = infer_data_roles(profile)
    result = {
        "profile": profile.to_dict(),
        "roles": roles,
        "quality": diagnose_data_quality(
            profile, roles, duplicate_rows=int(data.duplicated().sum())
        ),
    }
    assert result["quality"]["summary"]["issue_count"] == 3
    return result


@pytest.mark.parametrize(
    "text,expected",
    [
        (EXPLICIT_ANSWER, True),
        (EXPLICIT_ANSWER.replace("三个数据质量点", "八个数据质量点"), False),
        (CAPTURE["answer"], False),
        ("顺带提示三个数据质量点，供后续分步处理时参考：", False),
        (
            "顺带提示三个数据质量点，供后续分步处理时参考：\n"
            "- 检查字段名。\n- 核对单位。\n- 查看数据版本。",
            True,
        ),
        ("八个异常数据点均已核实。", False),
    ],
    ids=[
        "complete_diagnoses",
        "wrong_count",
        "ambiguous_duplicate",
        "outline",
        "actual_advice",
        "unsupported_anomalies",
    ],
)
def test_diagnostic_count_has_real_members(broadcast_profile, text, expected, tmp_path):
    result = checks.evaluate(broadcast_profile, text)
    evidence = [original.record(broadcast_profile)]
    candidate = validate_candidate(text, evidence, goal="复核画像")
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {"text": text, "result": result, "candidate": asdict(candidate)},
            ensure_ascii=False,
            indent=2,
        )
    )
    assert result["verification"]["passed"] is expected, result
    assert (result["report"]["status"] == "passed") is expected, result
    assert candidate.passed is expected, asdict(candidate)
    if expected:
        refs = [
            r
            for c in extract_numeric_claims(final_text=text, goal="复核画像", evidence=evidence)
            for r in c.value_refs
        ]
        count = next(r for r in refs if r.get("token") == "三")
        if text == EXPLICIT_ANSWER:
            assert count["semantic_binding"]["statistic"] == "quality_issue_count"
        else:
            assert count["quantity_purpose"] == "document"
            assert len(count["document_count_proof"]["members"]) == 3


@pytest.mark.parametrize("context", [None, 1, [], {}])
def test_malformed_source_context_cannot_complete(context):
    from tests.test_r1_post_review_contracts import projected

    evidence, _ = projected()
    evidence.summary["numeric_context"] = context
    text = "筛选后38行。"
    prepared = prepare_candidate(text, [evidence], goal="复核筛选")
    candidate = validate_candidate(text, [evidence], goal="复核筛选")
    assert not prepared.valid
    assert not candidate.passed
    if not isinstance(context, dict):
        assert "invalid_numeric_context" in candidate.issues
