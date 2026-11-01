"""R3 independent explicit field ownership, never borrow a field from a note cell."""

import json
from dataclasses import asdict

import pytest
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract

from tests.test_r3_independent_surface import FIELD, source  # noqa: F401


@pytest.mark.parametrize(
    "case,text,expected",
    [
        ("field-positive", f"|字段|备注|最大值|\n|---|---|---|\n|{FIELD}|无额外统计|8|", True),
        (
            "unknown-field-cannot-borrow-note",
            f"|字段|备注|最大值|\n|---|---|---|\n|不存在耗时（秒）|{FIELD}|8|",
            False,
        ),
        (
            "wrong-field-cannot-borrow-note",
            f"|字段|备注|最小值|\n|---|---|---|\n|等待（秒）|{FIELD}|2|",
            False,
        ),
        (
            "duplicate-field-header-conflict",
            f"|字段|字段|最大值|\n|---|---|---|\n|不存在耗时（秒）|{FIELD}|8|",
            False,
        ),
    ],
)
def test_explicit_field_owner_is_not_selected_from_any_equal_note(
    case,
    text,
    expected,
    source,  # noqa: F811
    tmp_path,  # noqa: F811
):  # noqa: F811
    record, artifact = source
    candidate = validate_candidate(text, [record], goal="复核表格事实")
    contract = build_minimal_contract(
        run_id="surface-run",
        user_text="复核表格事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[record],
        claims=list(candidate.claims),
    )
    try:
        report = validate_report_narratives([artifact], {"title": "表格复核", "insights": text})
    except ValueError as error:
        report = {"status": "rejected", "error": str(error)}
    witness = {
        "case": case,
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "report": report,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
    assert (report.get("status") == "passed") is expected, witness
