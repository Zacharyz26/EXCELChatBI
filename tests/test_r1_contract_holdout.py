"""Independent frozen R1 expectations: original evidence and explicit fact roles.

No model, product data, budget writes or new projection-schema assumptions.
Assertions precede implementation and must remain unchanged after the fix.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.analysis_requirements import AnalysisRequirements
from packages.orchestration.contracts import SuccessCriterion, TaskContract, build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import (
    ClaimDraft,
    EvidenceRecord,
    TaskDatasetBinding,
    TaskStepRecord,
    ToolInvocation,
)

FIXTURE = Path(__file__).parent / "fixtures" / "r1_independent_evidence.json"


@pytest.fixture(autouse=True)
def _offline_runtime(monkeypatch, tmp_path):
    """本组仅使用合成 Evidence；测试级禁止网络，不污染其他用例。"""

    def deny(*args, **kwargs):
        raise AssertionError("R1 independent tests forbid network and subprocesses")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _scene(name):
    # Fresh JSON data per call: consumers cannot mutate later test input.
    raw = json.loads(FIXTURE.read_text())[name]
    evidence = []
    for item in raw["evidence"]:
        item["source"] = json.loads(item.pop("source_json"))
        item["summary"] = json.loads(item.pop("summary_json"))
        evidence.append(EvidenceRecord(**item))
    return raw, evidence


def _claims(scene, text):
    raw, evidence = _scene(scene)
    claims = extract_numeric_claims(final_text=text, goal=raw["run"]["goal"], evidence=evidence)
    return raw, evidence, claims, [ref for claim in claims for ref in claim.value_refs]


def _verify_original(raw, evidence, text, claims):
    item = json.loads(raw["contracts"][0]["contract_json"])
    item["success_criteria"] = tuple(SuccessCriterion(**x) for x in item["success_criteria"])
    item["constraints"] = tuple(item["constraints"])
    item["assumptions"] = tuple(item["assumptions"])
    if item.get("analysis_requirements"):
        item["analysis_requirements"] = AnalysisRequirements.from_dict(
            item["analysis_requirements"]
        )
    invocations = []
    for item_inv in raw["invocations"]:
        item_inv = dict(item_inv)
        item_inv["args"] = json.loads(item_inv.pop("args_json"))
        invocations.append(ToolInvocation(**item_inv))
    steps = []
    for step in raw["steps"]:
        step = dict(step)
        step["definition"] = json.loads(step.pop("step_json"))
        step["logical_id"] = step["definition"]["step_id"]
        steps.append(TaskStepRecord(**step))
    fields = {f.name for f in dataclasses.fields(Artifact)}
    artifacts = []
    for artifact in raw["artifacts"]:
        value = {key: val for key, val in artifact.items() if key in fields}
        value["payload"] = json.loads(artifact["payload_json"])
        value["params"] = json.loads(artifact["params_json"])
        artifacts.append(Artifact(**value))
    return verify_completion(
        contract=TaskContract(**item),
        final_text=text,
        artifacts=artifacts,
        invocations=invocations,
        evidence=evidence,
        claims=claims,
        plan_steps=steps,
        dataset_bindings=[TaskDatasetBinding(**binding) for binding in raw["bindings"]],
    )


ROLE_CASES = [
    ("forward", "筛选前为75，筛选后为38。", True),
    ("reverse-order", "筛选后为38，筛选前为75。", True),
    ("swapped", "筛选前为38，筛选后为75。", False),
    ("swapped-reverse", "筛选后为75，筛选前为38。", False),
    ("row-units", "筛选前75行，筛选后38行。", True),
    ("wrong-row-units", "筛选前38行，筛选后75行。", False),
    ("colon", "筛选前：75，筛选后：38。", True),
    ("wrong-colon", "筛选前：38，筛选后：75。", False),
    ("bold", "**筛选前**：75；**筛选后**：38。", True),
    ("wrong-bold", "**筛选前**：38；**筛选后**：75。", False),
    ("quote", "确认如下：“筛选前为75，筛选后为38。”", True),
    ("wrong-quote", "确认如下：“筛选前为38，筛选后为75。”", False),
    ("linebreak", "筛选前为75。\n筛选后为38。", True),
    ("wrong-linebreak", "筛选前为38。\n筛选后为75。", False),
    ("wrong-operation", "去重前为75，去重后为38。", False),
    ("wrong-operation-clean", "清洗空值前为75，清洗空值后为38。", False),
]


@pytest.mark.parametrize("case,text,expected", ROLE_CASES, ids=[x[0] for x in ROLE_CASES])
def test_original_filter_role_and_full_verifier(case, text, expected):
    raw, evidence, claims, refs = _claims("s1-filter", text)
    assert len(refs) >= 2, refs
    actual = all(ref.get("supported") is True for ref in refs)
    verdict = _verify_original(raw, evidence, text, claims)
    assert actual is expected, refs
    assert verdict.passed is expected, dataclasses.asdict(verdict)
    if expected:
        for ref in refs:
            if ref.get("token") == "75":
                assert ref["path"] == "$.rows_before"
            elif ref.get("token") == "38":
                assert ref["path"] == "$.rows_after"


QUANTITY_CASES = [
    ("six", "本表覆盖六个月份，播出时长（分钟）的总计为1350。", "六", True),
    ("seven", "本表覆盖七个月份，播出时长（分钟）的总计为1350。", "七", False),
    ("eight", "本表覆盖八个月份，播出时长（分钟）的总计为1350。", "八", False),
    ("six-have", "本表共有六个月份，播出时长（分钟）的总计为1350。", "六", True),
    ("seven-have", "本表共有七个月份，播出时长（分钟）的总计为1350。", "七", False),
    ("six-groups", "本表共有六个分组，播出时长（分钟）的总计为1350。", "六", True),
    ("seven-groups", "本表共有七个分组，播出时长（分钟）的总计为1350。", "七", False),
    ("six-wrap", "本表覆盖六\n个月份，播出时长（分钟）的总计为1350。", "六", True),
    ("seven-wrap", "本表覆盖七\n个月份，播出时长（分钟）的总计为1350。", "七", False),
    ("unknown-half", "本表覆盖半个月份，播出时长（分钟）的总计为1350。", "半", False),
    ("unknown-object", "本表共有六个客户，播出时长（分钟）的总计为1350。", "六", False),
    ("wrong-basis", "本表共有六条记录，播出时长（分钟）的总计为1350。", "六", False),
]


@pytest.mark.parametrize(
    "case,text,quantity,expected", QUANTITY_CASES, ids=[x[0] for x in QUANTITY_CASES]
)
def test_quantity_is_preserved_and_bound_before_value_comparison(case, text, quantity, expected):
    raw, evidence, claims, refs = _claims("s5-chart", text)
    quantities = [ref for ref in refs if quantity in str(ref.get("token", ""))]
    assert quantities, refs
    assert all(ref.get("supported") is True for ref in refs) is expected, refs
    assert _verify_original(raw, evidence, text, claims).passed is expected
    if expected:
        assert all(ref["path"] == "$.group_total" for ref in quantities), quantities
    for claim in claims:
        for ref in claim.value_refs:
            if quantity not in str(ref.get("token", "")):
                continue
            span = ref.get("statement_span")
            assert span is not None, ref
            assert quantity in claim.statement[span[0] : span[1]], (claim, ref)


@pytest.mark.parametrize(
    "text",
    [
        "标签“七号方案”仅是名称；播出时长（分钟）的总计为1350。",
        "以下三个提示供参考；播出时长（分钟）的总计为1350。",
        "请按同一条说明核对；播出时长（分钟）的总计为1350。",
    ],
)
def test_ordinary_label_and_document_structure_do_not_create_data_facts(text):
    raw, evidence, claims, refs = _claims("s5-chart", text)
    # A03: a correct total does not fulfill an independently promised outline.
    pending = text == "以下三个提示供参考；播出时长（分钟）的总计为1350。"
    totals = [ref for ref in refs if ref["token"] == "1350"]
    assert len(totals) == 1 and totals[0]["supported"] is True, refs
    verdict = _verify_original(raw, evidence, text, claims)
    if pending:
        assert any(ref["token"] == "三" and ref.get("supported") is False for ref in refs)
        assert verdict.verdict == "NEEDS_ACTION", dataclasses.asdict(verdict)
    else:
        assert len(refs) == 1, refs
        assert verdict.passed


@pytest.mark.parametrize(
    "scene,text",
    [
        ("s2-clean", "清洗空值前为38，清洗空值后为37。"),
        ("s3-dedup", "去重前为37，去重后为36。"),
    ],
)
def test_other_operation_correct_roles_still_work(scene, text):
    _, _, _, refs = _claims(scene, text)
    assert len(refs) >= 2 and all(ref.get("supported") is True for ref in refs), refs


@pytest.mark.parametrize(
    "scene,text",
    [
        ("s2-clean", "清洗空值前为37，清洗空值后为38。"),
        ("s3-dedup", "去重前为36，去重后为37。"),
        ("s2-clean", "已删除1条记录。"),
        ("s3-dedup", "已删除1条重复记录。"),
        ("s3-dedup", "已删除0条重复记录。"),
        ("s3-dedup", "参与重复的记录数为2，重复组数为1。"),
    ],
)
def test_roles_and_missing_delta_cannot_borrow_profile_count(scene, text):
    _, _, _, refs = _claims(scene, text)
    assert refs and any(ref.get("supported") is not True for ref in refs), refs


@pytest.mark.parametrize("mode", ["none", "extracted", "fake-supported"])
@pytest.mark.parametrize(
    "scene,text",
    [
        ("s1-filter", "筛选后为75，筛选前为38。"),
        ("s5-chart", "本表覆盖八个月份，播出时长（分钟）的总计为1350。"),
    ],
)
def test_complete_verifier_rechecks_body_independently_of_supplied_claims(mode, scene, text):
    raw, evidence, extracted, _ = _claims(scene, text)
    claims = None if mode == "none" else extracted
    if mode == "fake-supported":
        claims = [
            ClaimDraft(
                statement=text,
                claim_kind="numeric",
                evidence_ids=(evidence[0].evidence_id,),
                value_refs=(
                    {"token": "1350", "supported": True, "evidence_id": evidence[0].evidence_id},
                ),
            )
        ]
    assert not _verify_original(raw, evidence, text, claims).passed


@pytest.mark.parametrize(
    "text,expected",
    [
        ("筛选前9行，筛选后4行。", True),
        ("筛选后4行，筛选前9行。", True),
        ("筛选前4行，筛选后9行。", False),
        ("筛选前为4，筛选后为9。", False),
    ],
)
def test_existing_summary_builder_handles_independently_constructed_nonhistorical_counts(
    text, expected
):
    # No new schema fields: the current producer's public result contract.
    result = {"parent_ref": "a" * 32, "dataset_ref": "b" * 32, "rows_before": 9, "rows_after": 4}
    args = {"dataset_ref": "a" * 32, "filters": [{"column": "设备", "op": "==", "value": "A"}]}
    summary = build_evidence_summary(summary="", result=result, artifact_id=None, arguments=args)
    record = EvidenceRecord(
        "independent-evidence",
        "independent-run",
        "independent-invocation",
        None,
        "tool_result",
        {"tool": "transform_dataset"},
        hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
        summary,
        "now",
    )
    claims = extract_numeric_claims(final_text=text, goal="核对工具中的统计事实", evidence=[record])
    refs = [ref for claim in claims for ref in claim.value_refs]
    assert len(refs) >= 2
    assert all(ref.get("supported") is True for ref in refs) is expected, refs
    contract = build_minimal_contract(
        run_id="independent-run",
        user_text="核对工具中的统计事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[record],
        claims=claims,
    )
    assert verdict.passed is expected, dataclasses.asdict(verdict)
