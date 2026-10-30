"""Full Verifier must validate raw delivery proof before argument enrichment."""

import copy
import dataclasses

import pytest
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract

from tests.test_r1_chart_delivery_cardinality import offline, original_inputs

__all__ = ["offline"]


@pytest.mark.parametrize(
    "mutation",
    [
        "context_encoding",
        "context_dataset",
        "context_chart_type",
        "context_null",
        "context_list",
        "context_missing",
        "source_dataset",
    ],
)
def test_raw_delivery_proof_is_not_repaired_into_support(mutation):
    evidence, calls, artifacts = original_inputs()
    summary = copy.deepcopy(evidence[2].summary)
    source = copy.deepcopy(evidence[2].source)
    if mutation == "context_encoding":
        summary["numeric_context"]["encoding"]["x"] = "错误维度"
    elif mutation == "context_dataset":
        summary["numeric_context"]["dataset_ref"] = "other-dataset"
    elif mutation == "context_chart_type":
        summary["numeric_context"]["chart_type"] = "line"
    elif mutation == "context_null":
        summary["numeric_context"] = None
    elif mutation == "context_list":
        summary["numeric_context"] = []
    elif mutation == "context_missing":
        summary.pop("numeric_context")
    elif mutation == "source_dataset":
        source["dataset_ref"] = "other-dataset"
    evidence[2] = dataclasses.replace(evidence[2], summary=summary, source=source)
    contract = build_minimal_contract(
        run_id=calls[0].run_id,
        user_text="核对已生成图表",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    result = verify_completion(
        contract=contract,
        final_text="三个维度图表均已生成。",
        evidence=evidence,
        invocations=calls,
        artifacts=artifacts,
        claims=None,
    )
    assert not result.passed, (mutation, dataclasses.asdict(result))


@pytest.mark.parametrize(
    "text",
    [
        "并非已生成三张图表。",
        "不是三个维度图表均已生成。",
    ],
)
def test_positive_delivery_count_does_not_prove_negative_assertion(text):
    evidence, calls, artifacts = original_inputs()
    contract = build_minimal_contract(
        run_id=calls[0].run_id,
        user_text="核对已生成图表",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    result = verify_completion(
        contract=contract,
        final_text=text,
        evidence=evidence,
        invocations=calls,
        artifacts=artifacts,
        claims=None,
    )
    assert not result.passed, (text, dataclasses.asdict(result))
