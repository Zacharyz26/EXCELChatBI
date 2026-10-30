"""Independent original-position and four-gate R1 holdouts; strictly offline.

Raw synthetic rows and explicit role oracles precede every product call. Public
builders encode Evidence only; no tested parser supplies expected positions or
verdicts. Existing tests and product implementation are left unchanged.
"""

from __future__ import annotations

import hashlib
import json
import socket
import statistics
from dataclasses import asdict
from typing import Any

import pytest
from apps.orchestrator.control import quantity_presentation
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.evidence_facts import seal_count_facts
from packages.session.task_models import EvidenceRecord, ToolInvocation

FIELD = "播出时长（分钟）"
OTHER = "停机时长（分钟）"
DATASET = "b" * 32
VERSION = "a" * 64
GOAL = "复核当前数据的已知事实"
ADVICE = f"建议确认后将{FIELD}的空值填充为0。"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("quantity source-map holdouts forbid network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _profile() -> EvidenceRecord:
    # Independent raw-data oracle: maximum40/null1 in each field, but minima
    # and medians differ. No product statistics are used to establish these.
    rows = {FIELD: [10, None, 40], OTHER: [40, 40, None]}
    columns = []
    for name, values in rows.items():
        valid = [value for value in values if value is not None]
        columns.append(
            {
                "name": name,
                "dtype": "float",
                "null_count": len(values) - len(valid),
                "null_ratio": (len(values) - len(valid)) / len(values),
                "distinct_count": len(set(valid)),
                "min": min(valid),
                "max": max(valid),
                "mean": statistics.mean(valid),
                "median": statistics.median(valid),
            }
        )
    result = {
        "profile": {"dataset_ref": DATASET, "row_count": 3, "col_count": 2, "columns": columns},
        "quality": {
            "schema": "chatbi-data-quality-v1",
            "mutates_data": False,
            "summary": {"issue_count": 1, "requires_confirmation": True},
            "issues": [
                {
                    "issue_id": "independent-missing",
                    "code": "missing_values",
                    "columns": [FIELD],
                    "evidence": {"null_count": 1},
                    "suggested_action": "fill_nulls",
                }
            ],
            "recommendations": [
                {
                    "issue_id": "independent-missing",
                    "columns": [FIELD],
                    "action": "fill_nulls",
                    "automatic": False,
                    "message": ADVICE,
                }
            ],
        },
    }
    summary = build_evidence_summary(
        summary="independent synthetic source-map fixture",
        result=result,
        artifact_id=None,
        arguments={"dataset_ref": DATASET, "data_version_hash": VERSION},
    )
    return EvidenceRecord(
        "profile-source",
        "source-run",
        "profile-invocation",
        None,
        "tool_result",
        {"tool": "get_data_profile"},
        _digest(result),
        summary,
        "2026-10-30T00:00:00Z",
    )


def _operation() -> tuple[EvidenceRecord, ToolInvocation]:
    # The source contract states before75, after38; removed37 is independently
    # derived and remains distinct from either population count.
    args = {"dataset_ref": "a" * 32, "filters": [{"column": "频道", "op": "==", "value": "A"}]}
    result = {
        "parent_ref": "a" * 32,
        "dataset_ref": DATASET,
        "rows_before": 75,
        "rows_after": 38,
        "transform": {"filters": args["filters"]},
        "row_changes": {
            "schema_version": 1,
            "removed_count": 75 - 38,
            "steps": [
                {
                    "operation": "filters",
                    "rows_before": 75,
                    "rows_after": 38,
                    "removed_count": 75 - 38,
                    "count_basis": "removed_rows",
                    "columns": None,
                }
            ],
        },
    }
    result_hash = _digest(result)
    summary = build_evidence_summary(
        summary="independent transform fixture",
        result=result,
        artifact_id=None,
        arguments={**args, "data_version_hash": VERSION},
    )
    summary = seal_count_facts(
        summary=summary,
        result=result,
        tool="transform_dataset",
        arguments=args,
        run_id="source-run",
        invocation_id="operation-invocation",
        result_hash=result_hash,
        branch_hash=VERSION,
    )
    evidence = EvidenceRecord(
        "operation-source",
        "source-run",
        "operation-invocation",
        None,
        "tool_result",
        {"tool": "transform_dataset"},
        result_hash,
        summary,
        "2026-10-30T00:00:00Z",
    )
    invocation = ToolInvocation(
        "operation-invocation",
        "source-run",
        None,
        "call",
        "transform_dataset",
        "key",
        _digest(args),
        args,
        "succeeded",
        result_hash,
        None,
        None,
        "now",
        "now",
    )
    return evidence, invocation


def _check(
    text: str,
    expected: bool,
    evidence: list[EvidenceRecord],
    invocations: list[ToolInvocation] | None = None,
):
    invocations = invocations or []
    candidate = validate_candidate(
        text,
        evidence,
        goal=GOAL,
        run_id="source-run",
        invocations=invocations,
    )
    claims = extract_numeric_claims(
        final_text=text,
        goal=GOAL,
        evidence=evidence,
        run_id="source-run",
        invocations=invocations,
    )
    contract = build_minimal_contract(
        run_id="source-run",
        user_text=GOAL,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    # No supplied ClaimDraft can authorize completion: exercise Verifier's own
    # public extract path again, instead of just testing the candidate wrapper.
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=invocations,
        evidence=evidence,
        claims=None,
    )
    assert candidate.passed is expected, asdict(candidate)
    assert verdict.passed is expected, asdict(verdict)
    return candidate, [ref for claim in claims for ref in claim.value_refs]


def _at(candidate, refs, text: str, start: int, token: str, *, role: str | None = None):
    expected_span = [start, start + len(token)]
    assert text[slice(*expected_span)] == token  # literal input-position oracle
    audits = [q for q in candidate.quantities if q.get("source_span") == expected_span]
    assert len(audits) == 1, (expected_span, candidate.quantities)
    audit = audits[0]
    assert audit["token"] == token
    assert audit["source_hash"] == hashlib.sha256(text.encode()).hexdigest()
    linked = [r for r in refs if r.get("source_span", r.get("statement_span")) == expected_span]
    assert linked and all(r.get("quantity_audit") == audit for r in linked), (linked, audit)
    if role:
        assert audit["quantity_purpose"] == "business"
        assert audit["binding"] == "supported"
        assert audit["obligation"] == "fulfilled"
        assert audit["semantic_binding"]["statistic"] == role
    return audit


@pytest.mark.parametrize(
    "label,token,role", [("最大值为", "40", "max"), ("空值数为", "1", "null_count")]
)
def test_plain_business_values_receive_complete_audit(label, token, role):
    prefix = FIELD + label
    text = prefix + token + "。"
    candidate, refs = _check(text, True, [_profile()])
    _at(candidate, refs, text, len(prefix), token, role=role)


def test_before_after_bindings_merge_with_surface_counts():
    first, middle = "处理结果：筛选前", "行，筛选后"
    text = first + "75" + middle + "38行。"
    evidence, invocation = _operation()
    candidate, refs = _check(text, True, [evidence], [invocation])
    before = _at(candidate, refs, text, len(first), "75")
    after = _at(candidate, refs, text, len(first + "75" + middle), "38")
    for audit, path in [(before, "$.rows_before"), (after, "$.rows_after")]:
        assert audit["quantity_purpose"] == "business"
        assert audit["binding"] == "supported" and audit["obligation"] == "fulfilled"
        assert audit["evidence_path"] == path


def test_equal_values_in_different_sentences_keep_different_occurrences():
    first, second = FIELD + "最大值为", OTHER + "最大值为"
    text = first + "40。\n" + second + "40。"
    candidate, refs = _check(text, True, [_profile()])
    a = _at(candidate, refs, text, len(first), "40", role="max")
    b = _at(candidate, refs, text, len(first + "40。\n" + second), "40", role="max")
    assert a["semantic_binding"]["object"]["column"] == FIELD
    assert b["semantic_binding"]["object"]["column"] == OTHER


def test_list_marker_same_text_never_steals_real_quantity_position():
    text = "1. 1."
    candidate, refs = _check(text, False, [_profile()])
    audit = _at(candidate, refs, text, 3, "1")
    assert audit["binding"] != "supported"
    assert not any(q.get("source_span") == [0, 1] for q in candidate.quantities)


@pytest.mark.parametrize(
    "prefix",
    [FIELD + "最大值为**", "**" + FIELD + "最大值**为**", "1. **" + FIELD + "最大值**为**"],
)
def test_markdown_bold_projection_preserves_original_value_span(prefix):
    text = prefix + "40**。"
    candidate, refs = _check(text, True, [_profile()])
    _at(candidate, refs, text, len(prefix), "40", role="max")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_table_equal_cells_keep_column_role_and_original_positions(newline):
    header = "|字段|最小值|最大值|" + newline + "|---|---|---|" + newline
    row_prefix = "|" + OTHER + "|"
    text = header + row_prefix + "40|40|" + newline
    candidate, refs = _check(text, True, [_profile()])
    first = _at(candidate, refs, text, len(header + row_prefix), "40", role="min")
    second = _at(candidate, refs, text, len(header + row_prefix + "40|"), "40", role="max")
    assert first["evidence_path"].endswith(".min")
    assert second["evidence_path"].endswith(".max")


@pytest.mark.parametrize("text", ["筛选前38行，筛选后75行。", "去重前75行，去重后38行。"])
def test_wrong_operation_roles_remain_rejected_after_audit_merge(text):
    evidence, invocation = _operation()
    candidate, refs = _check(text, False, [evidence], [invocation])
    assert any(q["binding"] != "supported" for q in candidate.quantities)
    assert any(ref.get("supported") is not True for ref in refs)


@pytest.mark.parametrize("actual_version,expected", [(VERSION, True), ("c" * 64, False)])
def test_explicit_version_identity_still_governs_same_value(actual_version, expected):
    prefix = f"数据集{DATASET}版本{actual_version}：{FIELD}最大值为"
    text = prefix + "40。"
    candidate, refs = _check(text, expected, [_profile()])
    audit = _at(candidate, refs, text, len(prefix), "40")
    assert (audit["binding"] == "supported") is expected


def test_advice_zero_does_not_prove_business_zero_in_another_sentence():
    candidate, refs = _check(ADVICE, True, [_profile()])
    zero = _at(candidate, refs, ADVICE, len(ADVICE) - 2, "0")
    assert zero["owner"] == "instruction_parameter"
    assert zero["quantity_purpose"] != "business"
    prefix = ADVICE + "\n\n" + FIELD + "空值数为"
    text = prefix + "0。"
    candidate, refs = _check(text, False, [_profile()])
    business = _at(candidate, refs, text, len(prefix), "0")
    assert business["binding"] != "supported"
    assert not business.get("advice_proof")


def test_public_extractor_and_full_verifier_fail_closed_on_uncovered_source(monkeypatch):
    # Deliberate local adapter fault: not a fake model result or a forged fact.
    # If audit issues are dropped, the already-correct max40 would wrongly pass.
    original = quantity_presentation.quantity_audit
    calls = []

    def uncovered(*args, **kwargs):
        quantities, issues = original(*args, **kwargs)
        calls.append(args[0])
        return quantities, (*issues, "independent_uncovered_source")

    monkeypatch.setattr(quantity_presentation, "quantity_audit", uncovered)
    text = FIELD + "最大值为40。"
    _candidate, refs = _check(text, False, [_profile()])
    assert len(calls) >= 3
    assert any(ref.get("reason") == "independent_uncovered_source" for ref in refs)
