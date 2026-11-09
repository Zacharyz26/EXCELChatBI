"""Independent common-audit boundaries, not a multi-run Host acceptance claim."""

import json

import pytest
from apps.orchestrator.control.quantity_presentation import quantity_audit

from tests.test_segmented_table_parent_independent import make_sealed_world, no_network

__all__ = ["no_network"]


@pytest.mark.parametrize(
    "case",
    [
        "single-source-identity",
        "unknown-scope-empty-claims",
        "ordinary-operation-description",
        "multi-source-unowned-metadata",
    ],
)
def test_source_declaration_common_audit(tmp_path, case):
    left = tmp_path / "left"
    left.mkdir()
    contract, _, _, evidence, metric, ref, version = make_sealed_world(left)
    text = f"数据集：{ref}，数据版本：{version}。"
    expected_issue = None
    if case == "unknown-scope-empty-claims":
        text = "筛选条件：未知列=A。"
        expected_issue = "unverified_source_declaration"
    elif case == "ordinary-operation-description":
        text = "筛选结果如下。"
    elif case == "multi-source-unowned-metadata":
        right = tmp_path / "right"
        right.mkdir()
        _, _, _, other, _, other_ref, _ = make_sealed_world(right)
        assert other_ref != ref
        evidence = [*evidence, *other]
        text += "\n\n" + f"| 月份 | {metric} |\n|---|---|\n| 2026-01 | 21 |"
        expected_issue = "unowned_source_declaration"
    quantities, issues = quantity_audit(text, evidence, [], goal=contract.goal)
    source_issues = [
        x for x in issues if x in {"unverified_source_declaration", "unowned_source_declaration"}
    ]
    (tmp_path / "source-declaration-audit.json").write_text(
        json.dumps(
            {
                "case": case,
                "text": text,
                "claims_input": [],
                "expected_issue": expected_issue,
                "quantities": quantities,
                "issues": issues,
                "source_issues": source_issues,
                "scope": (
                    "common quantity audit; real sealed fixtures; "
                    "not combined multi-run Host completion"
                ),
                "evidence": [
                    {
                        "id": e.evidence_id,
                        "run_id": e.run_id,
                        "source": e.source,
                        "result_hash": e.result_hash,
                    }
                    for e in evidence
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if expected_issue is None:
        assert not source_issues
    else:
        assert expected_issue in source_issues
