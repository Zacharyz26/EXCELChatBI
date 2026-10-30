"""Independent R1 post-review counterexamples with frozen manual oracles."""

import dataclasses
import hashlib
import json
import socket
from pathlib import Path

import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.evidence_facts import seal_count_facts
from packages.session.task_models import EvidenceRecord, ToolInvocation


@pytest.fixture(autouse=True)
def offline_review(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("offline R1 independent review")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def projected():
    args = {"dataset_ref": "a" * 32, "filters": [{"column": "频道", "op": "==", "value": "A"}]}
    result = {
        "dataset_ref": "b" * 32,
        "parent_ref": "a" * 32,
        "rows_before": 75,
        "rows_after": 38,
        "transform": {"filters": args["filters"]},
    }
    hashval = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    summary = build_evidence_summary(
        summary="",
        result=result,
        artifact_id=None,
        arguments={**args, "data_version_hash": "branch1"},
    )
    summary = seal_count_facts(
        summary=summary,
        result=result,
        tool="transform_dataset",
        arguments=args,
        run_id="run",
        invocation_id="iv",
        result_hash=hashval,
        branch_hash="branch1",
    )
    evidence = EvidenceRecord(
        "ev",
        "run",
        "iv",
        None,
        "tool_result",
        {"tool": "transform_dataset"},
        hashval,
        summary,
        "now",
    )
    iv = ToolInvocation(
        "iv",
        "run",
        None,
        "call",
        "transform_dataset",
        "key",
        "args",
        args,
        "succeeded",
        hashval,
        None,
        None,
        "now",
        "now",
    )
    return (evidence, iv)


def verdict(text, evidence, invocations=()):
    contract = build_minimal_contract(
        run_id=evidence[0].run_id,
        user_text="核对已完成变换的事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    claims = extract_numeric_claims(final_text=text, goal=contract.goal, evidence=evidence)
    vr = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=list(invocations),
        evidence=evidence,
        claims=claims,
    )
    return (vr, [r for c in claims for r in c.value_refs])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("筛选后为38。", True),
        ("筛选后最大值为38。", False),
        ("筛选后空值数为38。", False),
        ("筛选后38人。", False),
        ("筛选后记录数为38且最大值为38。", False),
        ("筛选前最小值为75。", False),
        ("筛选后播出时长为38分钟。", False),
        ("频道=B筛选后38行。", False),
        ("频道=A筛选后38行。", True),
        ("筛选后一共有38行。", True),
        ("筛选前一共有75行，筛选后一共有38行。", True),
        ("筛选后共有三十八行。", True),
    ],
)
def test_role_field_scope_unit_and_ordinary_word(text, expected):
    e, iv = projected()
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))


@pytest.mark.parametrize(
    "change",
    [
        "append",
        "projection_scalar",
        "projection_version",
        "path_mapping",
        "projection_hash",
        "fact_value",
    ],
)
def test_unsealed_or_malformed_projection_never_supports_claim(change):
    e, iv = projected()
    s = e.summary
    text = "筛选后为38。"
    if change == "append":
        text = "筛选后为999。"
        s["value_index"].append(
            {
                "path": "$.injected",
                "value": "999",
                "source_kind": "number",
                "fact": {"role": "after", "operation": "filters", "basis": "rows", "columns": None},
            }
        )
    elif change == "projection_scalar":
        s["fact_projection"] = 1
    elif change == "projection_version":
        s["fact_projection"]["schema"] = "future-v99"
    elif change == "path_mapping":
        s["fact_projection"]["paths"] = [{}]
    elif change == "projection_hash":
        s["fact_projection"]["projection_hash"] = "forged"
    elif change == "fact_value":
        s["value_index"][-1]["value"] = "999"
        text = "筛选后为999。"
    v, refs = verdict(text, [e], [iv])
    assert not v.passed, (refs, dataclasses.asdict(v))


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("覆盖三分之一个月份", False),
        ("覆盖一半个月份", False),
        ("覆盖七到八个月份", False),
        ("覆盖约七个月份", False),
        ("覆盖几个月份", False),
        ("覆盖七个月份", False),
        ("覆盖六个月份", True),
    ],
)
def test_compound_unknown_quantities_cannot_disappear(phrase, expected):
    raw = json.loads((Path(__file__).parent / "fixtures/r1_post_review_evidence.json").read_text())[
        "s5-chart"
    ]
    e = []
    for x in raw["evidence"]:
        x = dict(x)
        x["source"] = json.loads(x.pop("source_json"))
        x["summary"] = json.loads(x.pop("summary_json"))
        e.append(EvidenceRecord(**x))
    v, refs = verdict("本表" + phrase + "，播出时长（分钟）的总计为1350。", e)
    assert len(refs) > 1, refs
    assert v.passed is expected, (refs, dataclasses.asdict(v))
