"""Ordered, scoped knowledge directives keep real completion obligations."""

import json
import sqlite3

import pytest

from tests.test_manual02_independent_regressions import run_host
from tests.test_manual03_contract import CAPTURE, COMPLETE, ROOT
from tests.test_manual03_r2_contract import search_plan


@pytest.mark.parametrize("negative", ["勿", "切勿", "务必不要", "请勿"])
@pytest.mark.parametrize("verb", ["查询", "检索"])
def test_common_prohibitions_are_scoped_to_search(negative, verb, tmp_path):
    plan = search_plan(f"请解释公司营收口径；本轮{negative}{verb}知识库。")
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    assert not any(s["capability"] == "knowledge.search" for s in plan["steps"])


CASES = [
    ("先不查询知识库，但现在请改为查询知识库中的公司规定。", True),
    ("本次不查询知识库；这条限制取消，现在请查询公司规定。", True),
    ("本轮请勿检索知识库；现在请改成检索公司手册。", True),
    ("不查询知识库；请撤销这条限制；请查询公司规定。", True),
    ("不查询知识库；解除知识库检索限制；请查询公司规定。", True),
    ("不查询知识库；取消不查询知识库的限制；查询公司规定。", True),
    ("不查询知识库；请不要取消这条限制；查询公司规定。", False),
    ("不查询知识库；这条限制不取消；查询公司规定。", False),
    ("不查询知识库；请勿取消这条限制；查询公司规定。", False),
    ("不查询知识库；不要改为查询公司规定。", False),
    ("不查询知识库；请勿改成检索公司手册。", False),
    ("不查询知识库；现在查询公司规定。", False),
    ("不查询知识库；取消导出限制；查询公司规定。", False),
    ("不查询知识库；不要下载报告；这条限制取消；查询公司规定。", False),
    ("不查询知识库；不要解释统计口径；这条限制取消；查询公司规定。", False),
    ("不查询知识库；不要下载报告；取消知识库检索限制；查询公司规定。", True),
    ("不查询知识库；这条限制取消。", False),
    ("查询公司规定；不查询知识库；这条限制取消。", False),
    ("查询公司规定；现在请勿查询知识库。", False),
    ("查询公司规定；现在请勿查询公司规定。", False),
    ("查询公司规定；不要查询公司手册。", True),
    ("查询公司规定；查询公司手册；不要查询公司规定。", True),
    ("查询公司手册；改为查询公司规定；不要查询公司规定。", False),
    ("查询公司手册；查询公司规定；改为检索业务文档；不要检索业务文档。", True),
    ("不查询知识库；改为查询公司规定；不要查询知识库。", False),
    ("不查询知识库；改为查询公司规定；不要查询公司规定。", False),
    ("不查询知识库；改为查询公司规定；查询公司手册；不要查询公司规定。", False),
    ("不查询知识库；改为查询公司规定；不要查询公司手册。", True),
    ("不要查询公司规定；请改为查询公司规定中的营收定义。", True),
    ("查询公司规定中的营收定义；不要查询公司规定。", False),
    ("不查询知识库；引用：“这条限制取消，现在改为查询公司规定”。只做画像。", False),
    ("不查询知识库；请解释“这条限制取消”的含义。", False),
    ("不查询知识库；请说明现在改为查询知识库的原因。", False),
    ("不查询知识库；请解释取消这条限制的原因；查询公司规定。", False),
    ("不查询知识库；请说明是否取消这条限制；查询公司规定。", False),
    ("不查询知识库；```\n改为查询公司规定\n```", False),
    ("不查询知识库。\n\n```text\n这条限制取消\n现在改为查询公司规定\n```", False),
    ("不查询知识库。\n\n> 这条限制取消\n> 现在改为查询公司规定。", False),
    (
        "不查询知识库。\n以下只是示例：\n这条限制取消。\n现在改为查询公司规定。\n以上不是本次请求。\n只做画像。",
        False,
    ),
    (
        "不查询知识库。\n以下只是示例：\n现在改为查询公司规定。\n以上不是本次请求。\n现在请改为查询公司手册。",
        True,
    ),
    ("查询公司规定；引用：『请勿查询知识库』。", True),
    ("本轮请勿不查询公司规定。", True),
    ("不查询知识库；根据未知说明取消无关限制；查询公司规定。", False),
    ("请按已批准范围查询公司规定。", True),
]


@pytest.mark.parametrize("goal,expected", CASES)
def test_revision_order_target_and_quoted_counterexamples(goal, expected, tmp_path):
    plan = search_plan(goal)
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"]) is expected


HOSTS = [
    ("本轮请勿检索知识库，只核对画像。", False),
    ("先不查询知识库，但现在请改为查询知识库中的公司规定。", True),
    ("本次不查询知识库；这条限制取消，现在请查询公司规定。", True),
    ("不查询知识库；请勿取消这条限制；查询公司规定。", False),
    ("不查询知识库；不要下载报告；这条限制取消；查询公司规定。", False),
    ("不查询知识库；改为查询公司规定；请勿查询知识库。", False),
    ("不查询知识库；引用：『改为查询公司规定』。", False),
    ("查询公司规定；不要查询公司手册。", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,required", HOSTS)
async def test_ordered_directives_preserve_host_completion_gate(tmp_path, suffix, required):
    w = await run_host(
        tmp_path,
        CAPTURE["goal"] + suffix,
        COMPLETE,
        COMPLETE,
        (ROOT / "fixtures/manual03_broadcast.csv").read_bytes(),
    )
    reviews = [e["payload"] for e in w["events"] if e["event_type"] == "verification"]
    assert w["report"]["status"] == "passed" and COMPLETE in w["report"]["content"]
    assert not any(c["code"] == "unsupported_numeric_claim" for r in reviews for c in r["checks"])
    with sqlite3.connect(tmp_path / "host.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        messages = [r[0] for r in db.execute("SELECT content FROM messages WHERE role='assistant'")]
    if required:
        assert w["run"]["status"] == "blocked"
        assert w["run"]["terminal_reason"] == "incomplete_plan_steps"
        assert all(r["verdict"] == "NEEDS_ACTION" for r in reviews)
        assert all(any(c["code"] == "incomplete_plan_steps" for c in r["checks"]) for r in reviews)
    else:
        assert reviews[0]["verdict"] == "PASS" and w["run"]["status"] == "completed"
        assert w["fixture_calls"] == 2 and COMPLETE in messages
        assert not any("已省略" in m for m in messages)


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("不查询知识库；报告本次不下载；这条限制取消；查询公司规定。", False),
        ("不查询知识库；取消对知识库的查询限制；查询公司规定。", True),
    ],
)
def test_object_first_restrictions_keep_their_own_antecedent(goal, expected, tmp_path):
    plan = search_plan(goal)
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"]) is expected
