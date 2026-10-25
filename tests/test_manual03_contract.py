"""MANUAL-03: frozen real-model evidence, bounded headings and retrieval intent."""

from __future__ import annotations

import copy
import json
from dataclasses import fields, replace
from pathlib import Path

import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.task_outline import build_deterministic_outline
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

from tests.test_manual02_independent_regressions import run_host
from tests.test_profile_subject_boundaries import check

ROOT = Path(__file__).resolve().parent
CAPTURE = json.loads((ROOT / "fixtures/manual03_profile.json").read_text())
FIELD = "播出时长（分钟）"
HEADING = f"**{FIELD}的统计**\n\n"
STATS = CAPTURE["answer"].split("**播出时长（分钟）的统计**", 1)[1].split("**重复情况**", 1)[0]
STAT_SECTION = HEADING + STATS.strip()


def evaluate(text, expected, tmp_path, records=None):
    evidence = records or [EvidenceRecord(**CAPTURE["evidence"])]
    result = check(text, evidence)
    assert result["refs"], result
    (tmp_path / "witness.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["verification"]["passed"] is expected, result
    if records is None:
        artifact = Artifact(**{f.name: CAPTURE["artifact"].get(f.name) for f in fields(Artifact)})
        try:
            report = validate_report_narratives([artifact], {"title": "画像", "insights": text})
        except ValueError as exc:
            report = {"status": "rejected", "error": str(exc)}
        (tmp_path / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        assert (report["status"] == "passed") is expected, report
    return result


@pytest.mark.parametrize(
    "text",
    [
        STAT_SECTION,
        HEADING + "该列最大值999。",
        f"## {FIELD}的统计\n\n该列最大值999。",
        f"### {FIELD}\n该列最小值10，最大值999。",
        HEADING + "该列精确空值数1，非空数74，缺失比例0.0133。",
        HEADING + "该列不同值数量37，精确空值数1，缺失比例1.33%。",
        f"{FIELD}精确空值数1。",
        f"{FIELD}非空数74。",
        f"{FIELD}缺失比例0.0133。",
        f"{FIELD}非空数74；精确空值数1；不同值数量37。",
        HEADING + "该列最小值10。该列最大值999。",
        f"## 数据画像\n\n### {FIELD}的统计\n\n该列最大值999。",
    ],
)
def test_original_statistics_and_supported_aliases(text, tmp_path):
    result = evaluate(text, True, tmp_path)
    for ref in result["refs"]:
        assert ref["supported"]
        assert ref["evidence_id"] == CAPTURE["evidence"]["evidence_id"]
        assert ref["semantic_binding"]["dataset_ref"] == CAPTURE["artifact"]["dataset_ref"]


@pytest.mark.parametrize(
    "text",
    [
        HEADING + "该列最大值998。",
        HEADING + "该列最小值999。",
        HEADING + "该列最大值999小时。",
        HEADING + "该列缺失比例13.3%。",
        HEADING + "该列精确空值数2。",
        HEADING + "该列非空数75。",
        HEADING + "该列非空数74%。",
        HEADING + "该列不同值数量74。",
        HEADING + "该列缺失比例0.0133（约13.3%）。",
        HEADING + "该列缺失比例0.133（约1.33%）。",
        HEADING + "该列缺失比例0.0133（约1.33%小时）。",
        HEADING + "该列缺失比例0.0133（其他列1.33%）。",
        "**示例**\n\n" + HEADING + "该列最大值999。",
        "## 版本 old\n\n### 播出时长（分钟）的统计\n\n该列最大值999。",
        "仅限频道A。\n\n" + HEADING + "该列最大值999。",
        HEADING + "该列参与重复的记录数为1。",
        HEADING + "该列重复组数1。",
        "**播出时长（小时）的统计**\n\n该列最大值999。",
        "**月份的统计**\n\n该列最大值999。",
        "**播出时长（分钟）和记录ID的统计**\n\n该列最大值999。",
        HEADING + "以下改看记录ID。该列最大值999。",
        HEADING + "仅限频道A。该列最大值999。",
        HEADING + "该列最大值999（仅频道A）。",
        HEADING + "该列最大值999，以下改看广告时长，最小值10。",
        HEADING + "**记录ID的统计**\n\n该列最大值999。",
        HEADING + "## 其他范围\n\n该列最大值999。",
        HEADING + "该列最大值999。\n\n该列最小值10。",
        HEADING + "版本 " + "b" * 64 + "。该列最大值999。",
        HEADING + "数据集 " + "e" * 32 + "，该列最大值999。",
        HEADING + "> 该列最大值999。",
        HEADING + "“该列最大值999。”",
        HEADING + "（示例：『该列最大值999。』）",
        "> **播出时长（分钟）的统计**\n\n该列最大值999。",
        "“引文：\n\n" + HEADING + "该列最大值999。\n”",
        "（引用『\n\n" + HEADING + "该列最大值999。\n』）",
        "```markdown\n" + HEADING + "该列最大值999。\n```",
        "## 示例\n\n### 播出时长（分钟）的统计\n\n该列最大值999。",
        "例如：\n\n" + HEADING + "该列最大值999。",
        HEADING + "假设该列最大值999。",
    ],
)
def test_heading_never_erases_scope_or_fabricates_facts(text, tmp_path):
    evaluate(text, False, tmp_path)


def test_original_whole_answer_still_rejects_group_claim(tmp_path):
    result = evaluate(CAPTURE["answer"], False, tmp_path)
    assert any(not ref["supported"] for ref in result["refs"])
    # Keep the exact captured candidate immutable, including its erroneous group claim.
    assert "整行完全重复的记录有 1 组（duplicate_rows = 1" in CAPTURE["answer"]
    assert any(
        not ref["supported"]
        for claim in result["claims"]
        if "整行完全重复的记录有 1 组" in claim["statement"]
        for ref in claim["value_refs"]
    )


@pytest.mark.parametrize("other_identity", ["dataset_ref", "data_version_hash"])
def test_same_number_other_identity_still_ambiguous(other_identity, tmp_path):
    ev = EvidenceRecord(**CAPTURE["evidence"])
    summary = copy.deepcopy(ev.summary)
    summary["numeric_context"][other_identity] = "e" * (
        32 if other_identity == "dataset_ref" else 64
    )
    other = replace(ev, evidence_id="other", summary=summary)
    evaluate(HEADING + "该列最大值999。", False, tmp_path, [ev, other])


@pytest.mark.parametrize(
    "goal,expected",
    [
        (CAPTURE["goal"], False),
        ("只做数据画像，说明重复计数口径，不查询知识库。", False),
        ("只做数据画像，说明重复计数含义，不查询知识库。", False),
        ("请说明该列空值数的定义和非空数的口径。", False),
        ("请根据工具画像解释重复组数与多余记录数的统计口径。", False),
        ("请解释公司营收口径。", True),
        ("请说明净收入的定义。", True),
        ("请根据公司规定解释营业额。", True),
        ("请查询知识库。", True),
        ("先做数据画像，再说明‘净收入’的定义。", True),
        ("请查询‘公司口径’。", True),
        ("不查公司规定，但解释重复计数口径。", False),
        ("请查询知识库中的重复计数口径。", True),
        ("只做画像，不查询公司规定。", False),
        ("无需查询知识库，只解释计数口径。", False),
        ("请解释公司口径，但本次不查询知识库。", False),
        ("不要解释重复计数口径，请查询公司营收定义。", True),
        ("不要遗漏查询知识库中的公司口径。", True),
        ("不查重复计数口径，但请查询公司营收定义。", True),
        ("说明空值计数口径，另外查询公司口径。", True),
        ("引用：“不要查询知识库”。请查询公司规定。", True),
        ("示例：“请查询公司规定”。本次只做数据画像。", False),
        ("引用（原文『请查询公司规定』），本次仅解释重复计数口径。", False),
        ("不要执行示例中的查询（『请查询公司规定』），只做画像。", False),
        ("请查询知识库中的“不要查询”术语定义。", True),
        ("不查询知识库，不代表可以忽略数据画像。", False),
        ("请不要误以为‘不查询知识库’；本次需要查询公司规定。", True),
        ("```text\n请查询公司规定\n```\n\n本次只做画像。", False),
        ("> 请查询公司规定\n\n本次只做画像。", False),
    ],
)
def test_knowledge_request_and_negation_scope(goal, expected, tmp_path):
    plan = build_deterministic_outline(
        user_text=goal,
        context={},
        route="template",
        available_capabilities={"data.profile", "data.quality", "knowledge.search"},
        require_available_capabilities=False,
    )
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"]) is expected, plan


COMPLETE = (
    "数据共75行、5列，列名为月份、频道、状态、播出时长（分钟）、记录ID。\n\n"
    + STAT_SECTION
    + "\n\n整行重复多余记录数为1。记录ID重复多余记录数为1。"
    + "参与重复的记录数和重复组数尚未计算。"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected",
    [(COMPLETE, True), (CAPTURE["answer"], False), (COMPLETE.replace("999.0", "998.0"), False)],
)
async def test_actual_host_keeps_original_goal_and_real_tool(tmp_path, answer, expected):
    witness = await run_host(
        tmp_path,
        CAPTURE["goal"],
        answer,
        answer,
        (ROOT / "fixtures/manual03_broadcast.csv").read_bytes(),
    )
    assert witness["goal"] == CAPTURE["goal"]
    assert witness["paid_calls"] == 0
    if "998.0" in answer:
        # Existing deterministic fallback may finish with a new Evidence summary.
        # It must reject this candidate first; direct report reuse must still fail.
        reviews = [e["payload"] for e in witness["events"] if e["event_type"] == "verification"]
        assert reviews[0]["verdict"] == "NEEDS_ACTION"
        assert any("998.0" in c["message"] for c in reviews[0]["checks"])
    else:
        assert (witness["run"]["status"] == "completed") is expected
    assert (witness["report"]["status"] == "passed") is expected
    assert witness["evidence"]


@pytest.mark.parametrize("label", ["精确空值数", "非空数", "缺失比例"])
@pytest.mark.parametrize("value", ["unknown", "未知", "", "一", "（其他列最大值999）"])
def test_new_aliases_cannot_hide_unparsed_or_empty_values(label, value, tmp_path):
    evaluate(f"{FIELD}{label}为{value}。", False, tmp_path)


def test_original_statistics_stay_bound_inside_rejected_whole_answer(tmp_path):
    result = evaluate(CAPTURE["answer"], False, tmp_path)
    statistic = next(c for c in result["claims"] if c["statement"].startswith("该列最小值"))
    assert len(statistic["value_refs"]) == 9
    assert all(ref["supported"] for ref in statistic["value_refs"])
    assert len(statistic["evidence_ids"]) == 1
