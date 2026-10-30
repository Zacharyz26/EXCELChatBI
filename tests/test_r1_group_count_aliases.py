"""Independent frozen oracle: two groups, never nine; no provider or real DB IO."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import socket

import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.evidence_facts import seal_count_facts
from packages.session.task_models import EvidenceRecord, ToolInvocation


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a, **kw):
        raise AssertionError("offline projection review")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def source(state, injected=None, placement="caller"):
    # Independent complete data: two groups A/B, measures 40/30, total70.
    args = {"dataset_ref": "a" * 32, "group_col": "频道", "value_col": "时长", "agg": "sum"}
    result = {
        "rows": [{"group": "A", "value": 40}, {"group": "B", "value": 30}],
        "group_total": 2,
        "truncated": False,
        "group_col": "频道",
        "value_col": "时长",
        "agg": "sum",
        "total_value": 70,
    }
    if state == "unavailable":
        del result["group_total"]
    h = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    summary = build_evidence_summary(
        summary="two manually specified groups", result=result, artifact_id=None, arguments=args
    )
    if injected is not None and placement == "caller":
        summary["value_index"].append(injected)
    summary = seal_count_facts(
        summary=summary,
        result=result,
        tool="aggregate_preview",
        arguments=args,
        run_id="run",
        invocation_id="iv",
        result_hash=h,
        branch_hash="branch",
    )
    if injected is not None and placement == "persisted":
        summary["value_index"].append(injected)
    e = EvidenceRecord(
        "ev", "run", "iv", None, "tool_result", {"tool": "aggregate_preview"}, h, summary, "now"
    )
    iv = ToolInvocation(
        "iv",
        "run",
        None,
        "call",
        "aggregate_preview",
        "key",
        "args",
        args,
        "succeeded",
        h,
        None,
        None,
        "now",
        "now",
    )
    return e, iv


def check(e, iv, text):
    contract = build_minimal_contract(
        run_id="run",
        user_text="核对聚合事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    claims = extract_numeric_claims(final_text=text, goal=contract.goal, evidence=[e])
    result = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[iv],
        evidence=[e],
        claims=claims,
    )
    return result, [r for c in claims for r in c.value_refs]


@pytest.mark.parametrize("state", ["available", "unavailable"])
@pytest.mark.parametrize("placement", ["caller", "persisted"])
@pytest.mark.parametrize(
    "injected",
    [
        {"path": "$.other.group_total", "value": "9", "source_kind": "number"},
        {"path": "$.rows[0].group_total", "value": "9", "source_kind": "number"},
        {"path": "$.other", "value": "9", "source_kind": "number", "statistic": "group_total"},
    ],
    ids=["nested-metadata", "row-metadata", "explicit-role-alias"],
)
def test_unsealed_group_count_path_never_certifies_migrated_fact(
    state, placement, injected, tmp_path
):
    e, iv = source(state, dict(injected), placement)
    result, refs = check(e, iv, "共有九个频道。")
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {
                "state": state,
                "placement": placement,
                "evidence": dataclasses.asdict(e),
                "invocation": dataclasses.asdict(iv),
                "verdict": dataclasses.asdict(result),
                "refs": refs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    assert refs and any(r["token"] == "九" for r in refs), refs
    assert not result.passed, (refs, dataclasses.asdict(result))


@pytest.mark.parametrize(
    "state,text,expected",
    [
        ("available", "共有两个频道。", True),
        ("available", "共有三个频道。", False),
        ("unavailable", "共有两个频道。", False),
        ("available", "时长总计为70。", True),
        ("unavailable", "时长总计为70。", True),
    ],
)
def test_complete_counts_and_legacy_measure_controls(state, text, expected):
    e, iv = source(state)
    result, refs = check(e, iv, text)
    assert result.passed is expected, (refs, dataclasses.asdict(result))
