"""Corrected full-gate fixture: real transform, registration, SQLite bindings.

The original 75/38 synthetic cases stay frozen separately. This existing real
R2 five-row fixture independently yields A=4 and B=1, so B cannot claim4.
No product implementation, old input or safety expectation is changed.
"""

import json
from dataclasses import asdict

import pytest
from apps.orchestrator.control.quantity_presentation import validate_candidate
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe

from tests.test_r2_operation_completion import FILTER, RealTrace, no_network  # noqa: F401


@pytest.mark.parametrize("channel,expected", [("A", True), ("B", False)])
def test_parent_header_scope_is_checked_by_real_operation_gate(
    tmp_path, monkeypatch, channel, expected
):
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    trace = RealTrace(tmp_path, "筛选条件：频道=A。")
    original = load_dataframe(trace.source)
    assert len(original) == 5
    assert int((original["频道"] == "A").sum()) == 4
    assert int((original["频道"] == "B").sum()) == 1
    assert trace.admit(FILTER) is None
    result = trace.execute(FILTER)
    assert len(load_dataframe(trace.current)) == 4
    invocations, evidence, bindings = trace.state()
    text = (
        f"|筛选条件：频道={channel}（数据集{trace.current}，筛选后4行）|说明|\n"
        "|---|---|\n|已核对|完成|"
    )
    candidate = validate_candidate(
        text,
        evidence,
        goal=trace.contract.goal,
        run_id=trace.run.run_id,
        invocations=invocations,
    )
    verdict = trace.verify(text)
    witness = {
        "text": text,
        "expected": expected,
        "layer": "real deterministic transform and SQLite/full Verifier; no Host/model/browser",
        "oracle": {"source_rows": 5, "A_rows": 4, "B_rows": 1},
        "result": result,
        "contract": trace.contract.to_dict(),
        "invocations": [asdict(item) for item in invocations],
        "evidence": [asdict(item) for item in evidence],
        "bindings": [asdict(item) for item in bindings],
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    try:
        assert candidate.passed is expected, witness
        assert verdict.passed is expected, witness
    finally:
        get_settings.cache_clear()
