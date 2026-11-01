"""Independent R3 paired oracle; offline component paths, not real Agent.

The values and expectations are fixed from rows below before product calls.
No product parser/verdict supplies the oracle. Root executes this frozen file.
"""

import hashlib
import json
import socket
from dataclasses import asdict

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.config import get_settings
from packages.common.dataset_store import save_dataframe
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

FIELD = "耗时（秒）"
SECOND = "等待（秒）"
GOAL = "复核当前数据已有事实"


@pytest.fixture
def source(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("R3 independent surface tests prohibit network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    # Independent oracle: FIELD min2/max8/median4/null1 of4; SECOND min=max8.
    frame = pd.DataFrame({FIELD: [2.0, 4.0, 8.0, None], SECOND: [8.0, 8.0, None, None]})
    dataset = save_dataframe(frame)
    payload = {"profile": infer_schema({"dataset_ref": dataset}).to_dict()}
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    evidence = EvidenceRecord(
        "independent-profile",
        "surface-run",
        "surface-invocation",
        None,
        "tool_result",
        {"tool": "get_data_profile"},
        digest,
        build_evidence_summary(summary="independent local data", result=payload, artifact_id=None),
        "2026-11-01T00:00:00Z",
    )
    artifact = Artifact(
        "surface-artifact",
        "surface-conversation",
        "surface-message",
        "profile",
        payload,
        None,
        "get_data_profile",
        {"dataset_ref": dataset},
        dataset,
        "2026-11-01T00:00:00Z",
    )
    try:
        yield evidence, artifact
    finally:
        get_settings.cache_clear()


def check_all(source, text, expected, tmp_path):
    evidence, artifact = source
    candidate = validate_candidate(text, [evidence], goal=GOAL)
    claims = extract_numeric_claims(final_text=text, goal=GOAL, evidence=[evidence])
    contract = build_minimal_contract(
        run_id="surface-run",
        user_text=GOAL,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    complete = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[evidence],
        claims=claims,
    )
    try:
        report = validate_report_narratives([artifact], {"title": "统计说明", "insights": text})
        report_passed = report.get("status") == "passed"
    except ValueError as error:
        report, report_passed = {"error": str(error)}, False
    witness = {
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "complete": asdict(complete),
        "report": report,
        "claims": [asdict(c) for c in claims],
        "source": asdict(evidence),
        "layer": "offline component with real local profile tool; no Host/model/browser",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert complete.passed is expected, witness
    assert report_passed is expected, witness
    for claim in claims:
        for ref in claim.value_refs:
            if "source_span" in ref and not str(ref.get("token", "")).startswith("["):
                left, right = ref["source_span"]
                assert text[left:right] == ref.get("source_token", ref["token"]), witness
    return witness


FORMATS = [
    FIELD + "最小值2，最大值{value}。",
    "**" + FIELD + "**的**最小值**为**2**，**最大值**为**{value}**。",
    "### " + FIELD + "\n\n最小值2，最大值{value}。",
    "`" + FIELD + "`最小值`2`，最大值`{value}`。",
    '"（' + FIELD + "最小值2。" + FIELD + '最大值{value}）"。',
    "|字段|最小值|最大值|\n|---|---|---|\n|" + FIELD + "|2|{value}|\n",
]


@pytest.mark.parametrize("template", FORMATS)
@pytest.mark.parametrize("value,expected", [(8, True), (2, False)])
def test_equivalent_format_preserves_distinct_roles(source, tmp_path, template, value, expected):
    check_all(source, template.format(value=value), expected, tmp_path)


@pytest.mark.parametrize("quote", ['"', "'", "`", "「」"])
@pytest.mark.parametrize("assignment", [False, True])
def test_label_members_do_not_authorize_business_assignment(source, tmp_path, quote, assignment):
    a, b = quote[0], quote[-1]
    text = (
        f"{a}重复组数{b}为{a}八{b}。"
        if assignment
        else f"备注中的{a}重复组数：{b}{a}三成{b}均为标签。"
    )
    witness = check_all(source, text, not assignment, tmp_path)
    semantic = [
        r
        for c in witness["claims"]
        for r in c["value_refs"]
        if not str(r.get("token", "")).startswith("[")
    ]
    if assignment:
        assert any(
            r.get("token") == "八" and r.get("supported") is False for r in semantic
        ), witness
    else:
        assert semantic == [], witness


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_same_value_cells_are_bound_by_column_role_and_original_position(source, tmp_path, newline):
    text = newline.join(["|字段|最小值|最大值|", "|---|---|---|", f"|{SECOND}|8|8|", ""])
    witness = check_all(source, text, True, tmp_path)
    refs = [r for c in witness["claims"] for r in c["value_refs"] if r.get("supported") is True]
    assert {r["semantic_binding"]["statistic"] for r in refs} == {"min", "max"}, witness
    assert len({tuple(r["source_span"]) for r in refs}) == 2, witness


@pytest.mark.parametrize(
    "text,expected",
    [
        (f"画像已记录这一点——{FIELD}空值率25%。", True),
        (f"画像已记录这一点——{FIELD}空值率50%。", False),
        ("这里是两个标签：“甲”“乙”，另一名称是“附注”。", True),
        ("这里是两个标签：“甲”“乙”；本表另有八个异常数据点均已核实。", False),
        ("备注中的`重复组数` `三成`均为标签；另有五\n笔重复记录。", False),
        (f"{FIELD}有（\nb17\n）个缺失值。", False),
    ],
)
def test_local_structure_never_borrows_business_support(source, tmp_path, text, expected):
    check_all(source, text, expected, tmp_path)
