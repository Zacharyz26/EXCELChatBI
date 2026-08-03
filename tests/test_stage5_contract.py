"""Freeze guard for the stage-5 engineering acceptance scenarios."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / "evals" / "stage5" / "domain_definition_contract_v1.json"
MANIFEST = ROOT / "evals" / "stage5" / "manifest.json"


def test_stage5_scenario_set_matches_frozen_manifest() -> None:
    manifest = cast(dict[str, Any], json.loads(MANIFEST.read_text(encoding="utf-8")))
    payload = SCENARIO.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == manifest["scenario_sha256"]

    suite = cast(dict[str, Any], json.loads(payload))
    assert suite["version"] == 1
    assert suite["status"] == "engineering_baseline"
    assert suite["representativeness"] == {
        "raw_sensitive_data": False,
        "domain_specific_sales_assumptions": False,
        "product_owner_review": "pending",
        "note": "本集合冻结阶段 5 的工程契约；正式代表性工作负载仍需领域所有者确认。",
    }
    scenarios = cast(list[dict[str, Any]], suite["scenarios"])
    assert [item["id"] for item in scenarios] == [
        "S5-DEF-001",
        "S5-DEF-002",
        "S5-DEF-003",
        "S5-DEF-004",
        "S5-DEF-005",
        "S5-DEF-006",
    ]
    assert all(
        {
            "anonymous_schema",
            "typical_request",
            "required_artifact",
            "clarification_condition",
            "prohibited_behavior",
        }
        <= item.keys()
        for item in scenarios
    )
