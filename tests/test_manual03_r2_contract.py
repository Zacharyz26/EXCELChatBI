"""Copied into repository as a regression test before product fixes."""

import json
import sqlite3

import pytest
from apps.orchestrator.control.task_outline import build_deterministic_outline

from tests.test_manual02_independent_regressions import run_host
from tests.test_manual03_contract import CAPTURE, COMPLETE, HEADING, ROOT, evaluate


@pytest.mark.parametrize("label", ["缺失比例", "空值率", "缺失率", "null_ratio"])
@pytest.mark.parametrize("connector", ["为", "是", "：", "=", "为约"])
def test_ratio_assignment_keeps_two_separate_facts(label, connector, tmp_path):
    result = evaluate(HEADING + f"该列{label}{connector}0.0133（约1.33%）。", True, tmp_path)
    assert len(result["refs"]) == 2 and all(r["supported"] for r in result["refs"])
    assert {r["semantic_binding"]["statistic"] for r in result["refs"]} == {"null_ratio"}


@pytest.mark.parametrize("connector", ["为", "是", "："])
@pytest.mark.parametrize(
    "body",
    [
        "该列缺失比例{c}0.133（约1.33%）。",
        "该列缺失比例{c}0.0133（约13.3%）。",
        "该列缺失比例{c}0.0133（约1.33%小时）。",
        "该列缺失比例{c}未知（约1.33%）。",
        "该列非空数{c}0.0133（约1.33%）。",
        "记录ID缺失比例{c}0.0133（约1.33%）。",
        "筛选后该列缺失比例{c}0.0133（约1.33%）。",
    ],
)
def test_ratio_assignment_does_not_relax_identity_or_values(connector, body, tmp_path):
    evaluate(HEADING + body.format(c=connector), False, tmp_path)


def search_plan(goal):
    return build_deterministic_outline(
        user_text=goal,
        context={},
        route="template",
        available_capabilities={"data.profile", "data.quality", "knowledge.search"},
        require_available_capabilities=False,
    )


@pytest.mark.parametrize("negative", ["不需要", "不用", "无需", "不必", "不要", "禁止"])
@pytest.mark.parametrize("before", [False, True])
def test_global_search_prohibition_tracks_its_object(negative, before, tmp_path):
    command = f"本次知识库{negative}查询" if before else f"本次{negative}查询知识库"
    plan = search_plan("请解释公司营收口径，" + command + "。")
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    assert not any(s["capability"] == "knowledge.search" for s in plan["steps"])


@pytest.mark.parametrize("outer", ["不要", "不能", "并非", "不是", "不可能", "不需要"])
def test_composed_negative_preserves_business_obligation(outer, tmp_path):
    plan = search_plan(f"请{outer}不查询知识库中的营收定义。")
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"])


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("请说明不用查询知识库的原因，本次只做数据画像。", False),
        ("请解释无需查询公司规定的原因，本次只做画像。", False),
        ("请说明为什么不需要查询公司规定。", False),
        ("请说明查询知识库是否必要。", False),
        ("请解释公司规定为什么要求查询知识库。", True),
        ("请说明不查询知识库的原因，但请查询公司规定。", True),
        ("不要解释无需查询知识库的原因，请查询公司规定。", True),
        ("请不要遗漏查询知识库中的营收定义。", True),
        ("不确定是否查询知识库中的公司规定，请保留这项请求。", True),
        ("请查询公司规定，但本次知识库不需要查询。", False),
        ("请说明“不要不查询知识库”是什么意思。", False),
        ("引用：“本次不需要查询知识库”。请查询公司规定。", True),
        ("不要说明公司口径，只解释空值数的统计含义。", False),
        ("以下只是示例：\n请查询公司规定。\n以上不是本次请求。\n本次只做数据画像。", False),
        ("以下只是示例：\n请查询公司规定。\n以上不是本次请求。\n请查询公司营收定义。", True),
        ("以下只是示例：\n请查询公司规定。", True),
        ("不需要解释统计口径，请检索公司手册。", True),
        ("请说明空值统计口径并查询公司规定。", True),
    ],
)
def test_predicate_scope_and_unknown_intent_are_not_discarded(goal, expected, tmp_path):
    plan = search_plan(goal)
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"]) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,assigned,search",
    [
        ("", True, False),
        ("本次不需要查询知识库。", False, False),
        ("本次知识库不查询。", False, False),
        ("请说明不用查询知识库的原因。", False, False),
        ("请不要不查询公司规定。", False, True),
    ],
)
async def test_host_keeps_statistics_and_unfinished_query_obligations(
    tmp_path, suffix, assigned, search
):
    answer = COMPLETE.replace("缺失比例 0.0133", "缺失比例为0.0133") if assigned else COMPLETE
    w = await run_host(
        tmp_path,
        CAPTURE["goal"] + suffix,
        answer,
        answer,
        (ROOT / "fixtures/manual03_broadcast.csv").read_bytes(),
    )
    reviews = [e["payload"] for e in w["events"] if e["event_type"] == "verification"]
    assert w["report"]["status"] == "passed"
    assert answer in w["report"]["content"]
    with sqlite3.connect(tmp_path / "host.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        messages = [r[0] for r in db.execute("SELECT content FROM messages WHERE role='assistant'")]
    if search:
        assert w["run"]["status"] == "blocked"
        assert w["run"]["terminal_reason"] == "incomplete_plan_steps"
        assert all(r["verdict"] == "NEEDS_ACTION" for r in reviews)
        assert all(any(c["code"] == "incomplete_plan_steps" for c in r["checks"]) for r in reviews)
        assert not any(
            c["code"] == "unsupported_numeric_claim" for r in reviews for c in r["checks"]
        )
    else:
        assert reviews[0]["verdict"] == "PASS"
        assert w["run"]["status"] == "completed" and w["fixture_calls"] == 2
        assert answer in messages
        assert not any("已省略" in m for m in messages)


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("不需要" * 128, True),
        ("不需要" * 129, False),
        ("不需要" * 2001 + "未判明是否", True),
    ],
)
def test_long_operator_chains_keep_polarity_without_backtracking(prefix, expected):
    plan = search_plan(prefix + "查询知识库中的公司规定。")
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"]) is expected
