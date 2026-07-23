"""Deterministic numeric Claim extraction and Evidence linking tests."""

from __future__ import annotations

from apps.orchestrator.control.claims import (
    build_evidence_summary,
    extract_claims,
    extract_knowledge_claims,
    extract_numeric_claims,
)
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_models import EvidenceRecord


def _evidence(result: object) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="evidence-1",
        run_id="run-1",
        invocation_id="invocation-1",
        artifact_id=None,
        kind="tool_result",
        source={"tool": "get_data_profile"},
        result_hash="hash",
        summary=build_evidence_summary(
            summary="数据画像完成",
            result=result,
            artifact_id=None,
        ),
        created_at="now",
    )


def test_numeric_claims_link_each_value_to_evidence_path() -> None:
    evidence = _evidence(
        {"profile": {"row_count": 3}, "quality": {"duplicate_rows": 0}}
    )

    claims = extract_numeric_claims(
        final_text="数据共 3 行，重复记录 0 行。",
        goal="检查数据质量",
        evidence=[evidence],
    )

    assert len(claims) == 1
    assert claims[0].evidence_ids == ("evidence-1",)
    assert [ref["supported"] for ref in claims[0].value_refs] == [True, True]
    assert [ref["path"] for ref in claims[0].value_refs] == [
        "$.profile.row_count",
        "$.quality.duplicate_rows",
    ]


def test_percentage_conversion_is_not_inferred_but_tool_display_value_can_link() -> None:
    ratio_only = _evidence({"rate": 0.251})

    claims = extract_numeric_claims(
        final_text="转化率为 25.1%。",
        goal="计算转化率",
        evidence=[ratio_only],
    )

    assert claims[0].value_refs[0]["supported"] is False

    with_display_value = _evidence({"rate": 0.251, "display": "25.1%"})
    linked = extract_numeric_claims(
        final_text="转化率为 25.1%。",
        goal="计算转化率",
        evidence=[with_display_value],
    )
    assert linked[0].value_refs[0]["supported"] is True
    assert linked[0].value_refs[0]["path"] == "$.display#number[0]"


def test_unsupported_numeric_claim_fails_completion_verification() -> None:
    evidence = _evidence({"profile": {"row_count": 3}})
    claims = extract_numeric_claims(
        final_text="数据共 4 行。",
        goal="检查数据规模",
        evidence=[evidence],
    )
    contract = build_minimal_contract(
        run_id="run-1",
        user_text="检查数据规模",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    result = verify_completion(
        contract=contract,
        final_text="数据共 4 行。",
        artifacts=[],
        invocations=[],
        evidence=[evidence],
        claims=claims,
    )

    assert result.verdict == "NEEDS_ACTION"
    assert [issue.code for issue in result.issues] == ["unsupported_numeric_claim"]


def test_goal_scope_numbers_and_identifier_suffixes_are_not_claims() -> None:
    claims = extract_numeric_claims(
        final_text="已按 2024 年范围完成，报告编号为 report-1。",
        goal="分析 2024 年数据",
        evidence=[],
    )

    assert claims == []


def test_goal_equal_result_number_is_not_blanket_exempted() -> None:
    claims = extract_numeric_claims(
        final_text="最终销售额是 2024 元。",
        goal="分析 2024 年销售额",
        evidence=[],
    )

    assert claims[0].value_refs[0]["supported"] is False


def test_scientific_notation_is_extracted_and_linked() -> None:
    evidence = _evidence({"p_value": 0.000012})

    claims = extract_numeric_claims(
        final_text="检验的 p 值为 1.2e-5。",
        goal="执行显著性检验",
        evidence=[evidence],
    )

    assert claims[0].value_refs[0]["supported"] is True
    assert claims[0].value_refs[0]["path"] == "$.p_value"


def test_same_value_paths_prefer_semantically_matching_field_and_keep_candidates() -> None:
    evidence = _evidence(
        {"profile": {"row_count": 3}, "quality": {"duplicate_rows": 3}}
    )

    claims = extract_numeric_claims(
        final_text="重复记录有 3 行。",
        goal="检查数据质量",
        evidence=[evidence],
    )

    ref = claims[0].value_refs[0]
    assert ref["path"] == "$.quality.duplicate_rows"
    assert ref["ambiguous_value"] is True
    assert ref["candidate_paths"] == [
        {"evidence_id": "evidence-1", "path": "$.profile.row_count"},
        {"evidence_id": "evidence-1", "path": "$.quality.duplicate_rows"},
    ]


def test_knowledge_claim_requires_an_explicit_returned_source() -> None:
    evidence = EvidenceRecord(
        evidence_id="knowledge-1",
        run_id="run-1",
        invocation_id="invocation-1",
        artifact_id=None,
        kind="tool_result",
        source={"tool": "kb_search"},
        result_hash="hash",
        summary=build_evidence_summary(
            summary="知识库命中 1 条",
            result={
                "is_empty": False,
                "hits": [
                    {
                        "source": "指标口径.md",
                        "section": "活跃用户",
                        "text": "活跃用户指有效登录的去重用户数。",
                    }
                ],
            },
            artifact_id=None,
        ),
        created_at="now",
    )

    unsupported = extract_knowledge_claims(
        final_text="活跃用户指有效登录的去重用户数。", evidence=[evidence]
    )
    linked = extract_knowledge_claims(
        final_text="活跃用户指有效登录的去重用户数（来源：指标口径.md）。",
        evidence=[evidence],
    )

    assert unsupported[0].value_refs[0]["supported"] is False
    assert linked[0].evidence_ids == ("knowledge-1",)
    assert linked[0].value_refs[0]["source"] == "指标口径.md"

    contract = build_minimal_contract(
        run_id="run-1",
        user_text="活跃用户怎么定义",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    result = verify_completion(
        contract=contract,
        final_text="活跃用户指有效登录的去重用户数。",
        artifacts=[],
        invocations=[],
        evidence=[evidence],
        claims=unsupported,
    )
    assert [issue.code for issue in result.issues] == ["unsupported_knowledge_claim"]


def test_empty_knowledge_result_only_supports_an_honest_no_result_answer() -> None:
    evidence = EvidenceRecord(
        evidence_id="knowledge-empty",
        run_id="run-1",
        invocation_id="invocation-1",
        artifact_id=None,
        kind="tool_result",
        source={"tool": "kb_search"},
        result_hash="hash",
        summary=build_evidence_summary(
            summary="知识库没有命中",
            result={"is_empty": True, "hits": []},
            artifact_id=None,
        ),
        created_at="now",
    )

    honest = extract_claims(
        final_text="知识库中未找到相关内容，无法回答。",
        goal="查询内部定义",
        evidence=[evidence],
    )
    fabricated = extract_claims(
        final_text="公司口径规定该指标按自然月计算。",
        goal="查询内部定义",
        evidence=[evidence],
    )

    assert honest[0].claim_kind == "knowledge"
    assert honest[0].value_refs[0]["supported"] is True
    assert fabricated[0].value_refs[0]["supported"] is False


def test_explicit_limitation_is_persistable_claim_metadata() -> None:
    claims = extract_claims(
        final_text="结论仅供参考，样本不足是主要局限。",
        goal="总结分析",
        evidence=[],
    )

    assert claims[0].claim_kind == "limitation"
    assert claims[0].value_refs[0]["confidence"] == "explicitly_qualified"
