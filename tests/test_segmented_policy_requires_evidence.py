"""Legacy/unknown policy projections cannot certify an ordinal paraphrase."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from apps.orchestrator.control.quantity_presentation import prepare_candidate

from tests.test_r2_operation_completion import RealTrace, no_network

__all__ = ["no_network"]


@pytest.mark.parametrize("policy", ["首条", "第一条", "第1条"])
def test_missing_policy_remains_unknown_for_equivalent_first(tmp_path, policy):
    trace = RealTrace(tmp_path, "按整行去重。")
    trace.execute({"drop_duplicates": []})
    record = trace.state()[1][0]
    summary = deepcopy(record.summary)
    projection = summary["fact_projection"]
    projection.pop("policies", None)
    envelope = {k: v for k, v in projection.items() if k != "projection_hash"}
    facts = [f for f in summary["value_index"] if f["path"] in projection["paths"]]
    projection["projection_hash"] = hashlib.sha256(
        json.dumps(
            {"envelope": envelope, "facts": facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    legacy = replace(record, summary=summary)
    text = "每组保留" + policy + "。"
    candidate = prepare_candidate(text, [legacy], goal="按整行去重。", run_id=record.run_id)
    (tmp_path / "missing-policy.json").write_text(
        json.dumps(
            {"text": text, "candidate": asdict(candidate), "legacy_evidence": asdict(legacy)},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    assert not candidate.valid
