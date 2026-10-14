"""Fixed regression gate for legacy intent regex vs generic postconditions."""

from __future__ import annotations

import pytest
from apps.orchestrator.agent_loop import (
    _requests_chart,
    _requests_pdf,
    _requests_report,
)
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.verifier import verify_completion


@pytest.mark.parametrize(
    ("text", "chart", "report", "pdf"),
    [
        ("请生成销售折线图", True, False, False),
        ("visualize sales by month", True, False, False),
        ("不要图表，只给文字结论", False, False, False),
        ("给我一份销售分析报告", False, True, False),
        ("请把分析组装成报告并导出 PDF", False, True, True),
        ("生成报告，但不要 PDF", False, True, False),
        ("报告通常包含什么？", False, False, False),
        ("请基于工具 Evidence 报告总销售额和各地区销售额", False, False, False),
        ("请引用已有报告说明结论", False, False, False),
        ("Please report total sales by region", False, False, False),
        ("Create a new sales report", False, True, False),
        ("Use the previous report to create a new PDF report", False, True, True),
        ("Export the existing report", False, False, False),
        ("基于已有报告生成一份新报告", False, True, False),
        ("Create a report, but not a PDF", False, True, False),
        ("Don't create a report", False, False, False),
        ("Do not generate a report; summarize the findings", False, False, False),
        ("Don't export a PDF; just summarize.", False, False, False),
        ("不要使用之前报告，请根据当前数据生成新报告。", False, True, False),
        ("Use the previous report to create an updated report.", False, True, False),
        ("基于已有报告重新生成报告", False, True, False),
        ("Download the previous PDF", False, False, False),
        ("导出已有 PDF", False, False, False),
        ("画趋势图并生成报告", True, True, False),
        ("只分析趋势，不需要图表或报告", False, False, False),
    ],
)
def test_legacy_intent_and_contract_postcondition_shadow_are_equivalent(
    text: str,
    chart: bool,
    report: bool,
    pdf: bool,
) -> None:
    """Every legacy positive must compile to a hard, generic Artifact criterion."""
    regex_chart = _requests_chart(text)
    regex_report = _requests_report(text)
    regex_pdf = regex_report and _requests_pdf(text)
    assert (regex_chart, regex_report, regex_pdf) == (chart, report, pdf)

    contract = build_minimal_contract(
        run_id="shadow-run",
        user_text=text,
        chart_required=regex_chart,
        report_required=regex_report,
        pdf_required=regex_pdf,
    )
    artifact_criteria = {
        (item.artifact_type, item.artifact_format)
        for item in contract.success_criteria
        if item.kind == "artifact"
    }
    expected = set()
    if chart:
        expected.add(("chart", None))
    if report:
        expected.add(("report", "pdf" if pdf else None))
    assert artifact_criteria == expected

    result = verify_completion(
        contract=contract,
        final_text="分析完成。",
        artifacts=[],
        invocations=[],
        evidence=[],
    )
    issue_codes = {item.code for item in result.issues}
    assert ("missing_chart_artifact" in issue_codes) is chart
    assert ("missing_report_artifact" in issue_codes) is report
    total_required = text in {
        "请基于工具 Evidence 报告总销售额和各地区销售额",
        "Please report total sales by region",
    }
    assert ("missing_required_total_evidence" in issue_codes) is total_required
    assert result.passed == (not (chart or report or total_required))
