"""Independent R3 source/parser parity with the project's actual Markdown processor."""

import json
from dataclasses import asdict

import markdown
import pytest
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.common.text_surface import parse_text_surface
from packages.orchestration.contracts import build_minimal_contract


@pytest.mark.parametrize(
    "text",
    [
        "`甲``乙`",
        "`甲` `乙`",
        "``甲`乙``",
        "`甲``乙",
        "``甲`乙",
        "备注中的`重复组数：``三成`均为标签。",
        "备注中的`重复组数：` `三成`均为标签。",
    ],
)
def test_code_nodes_match_actual_markdown_processor_source(text, tmp_path):
    converter = markdown.Markdown(extensions=["tables", "fenced_code"])
    processor = converter.inlinePatterns["backtick"]
    actual = []
    for match in processor.getCompiledRegExp().finditer(text):
        element, left, right = processor.handleMatch(match, text)
        if not isinstance(element, str):
            actual.append((left, right, match.start(3), match.end(3)))
    nodes = [n for n in parse_text_surface(text).nodes if n.kind == "inline_code"]
    observed = [(n.span[0], n.span[1], n.content_span[0], n.content_span[1]) for n in nodes]
    witness = {
        "text": text,
        "actual_processor_spans": actual,
        "surface_spans": observed,
        "rendered_html": converter.convert(text),
        "nodes": [asdict(n) for n in nodes],
        "oracle": "installed Markdown backtick processor (independent of TextSurface)",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert observed == actual, witness


@pytest.mark.parametrize(
    "case,text,expected",
    [
        ("spaced-code-labels", "备注中的`重复组数：` `三成`均为标签。", True),
        ("single-code-assignment", "`重复组数：``八`。", False),
        ("single-code-assignment-with-label-prefix", "备注中的`重复组数：``八`均为标签。", False),
        ("external-assignment", "`重复组数`为`八`。", False),
        (
            "spaced-labels-plus-business",
            "备注中的`重复组数：` `三成`均为标签；另有五笔重复记录。",
            False,
        ),
    ],
)
def test_backticks_cannot_invent_sibling_labels_to_erase_business_numbers(
    case, text, expected, tmp_path
):
    candidate = validate_candidate(text, [], goal="复核数量")
    contract = build_minimal_contract(
        run_id="backtick-contract",
        user_text="复核数量",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[],
        claims=list(candidate.claims),
    )
    try:
        report = validate_report_narratives(
            [], {"title": "数量复核", "insights": text}, run_id="backtick-contract"
        )
    except ValueError as error:
        report = {"status": "rejected", "error": str(error)}
    witness = {
        "case": case,
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "report": report,
        "model_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
    assert (report.get("status") == "passed") is expected, witness
    if not expected:
        # Business/unknown quantities must still exist, not be erased as a label.
        assert candidate.quantities, witness
        assert any(q.get("token") in {"八", "五"} for q in candidate.quantities), witness
