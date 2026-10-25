"""Object identity and typed directive withdrawal across grammatical variants."""

import json
import sqlite3

import pytest

from tests.test_manual02_independent_regressions import run_host
from tests.test_manual03_contract import CAPTURE, COMPLETE, ROOT
from tests.test_manual03_r2_contract import search_plan


def assert_search(goal, expected, tmp_path):
    plan = search_plan(goal)
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    assert any(s["capability"] == "knowledge.search" for s in plan["steps"]) is expected


@pytest.mark.parametrize("verb", ["查询", "检索", "查阅"])
@pytest.mark.parametrize("time", ["本轮", "本次", "现在", "当前"])
@pytest.mark.parametrize("placement", ["before", "after"])
@pytest.mark.parametrize("same", [True, False])
def test_temporal_modifiers_do_not_change_object_identity(verb, time, placement, same, tmp_path):
    name = "公司规定" if same else "公司手册"
    target = time + name if placement == "before" else name + time
    assert_search(f"请查询公司规定；{target}请勿{verb}。", not same, tmp_path)


IDENTITIES = [
    ("公司规定", "“公司规定”", False),
    ("“公司规定”", "公司规定", False),
    ("“公司规定”", '"公司规定"', False),
    ("『公司规定』", "‘公司规定’", False),
    ("《公司规定》", "`公司规定`", False),
    ("“公司规定”", "“公司手册”", True),
    ("“本轮公司规定”", "“公司规定”", True),
    ("“本轮公司规定”", "『本轮公司规定』", False),
    ("“公司规定本轮”", "“公司规定”", True),
    ("“公司暂时规定”", "“公司规定”", True),
    ("“不查询公司规定”", "“公司规定”", True),
    ("“不查询公司规定”", "『不查询公司规定』", False),
    ("“公司规定；不查询知识库”", "“公司规定”", True),
    ("“公司规定；不查询知识库”", "『公司规定；不查询知识库』", False),
    ("公司规定", "“*”", True),
    ("“公司规定”中的营收定义", "公司规定", False),
    ("知识库中的“公司规定”", "公司规定", False),
    ("公司规定", "知识库中的“公司规定”", False),
    ("“公司规定”", "知识库", False),
    ("“公司规定”", "本轮“公司规定”", False),
    ("“公司规定”", "“公司规定”本轮", False),
]


@pytest.mark.parametrize("requested,prohibited,expected", IDENTITIES)
def test_delimiters_preserve_literal_names_and_external_instructions(
    requested, prohibited, expected, tmp_path
):
    assert_search(f"请查询{requested}；现在请勿查询{prohibited}。", expected, tmp_path)


@pytest.mark.parametrize("verb", ["取消", "撤销", "撤回", "收回"])
@pytest.mark.parametrize("noun", ["要求", "请求", "任务"])
@pytest.mark.parametrize("negated", [False, True])
def test_positive_requests_have_their_own_withdrawal(verb, noun, negated, tmp_path):
    prefix = "不要" if negated else ""
    assert_search(f"请检索『公司规定』；请{prefix}{verb}查询公司规定的{noun}。", negated, tmp_path)


CASES = [
    ("查询公司规定；本轮查询公司规定的要求撤销。", False),
    ("查询公司规定；查询公司规定的要求本轮撤回。", False),
    ("查询公司规定；撤销对公司规定的查询要求。", False),
    ("查询公司规定；撤销公司规定本轮查询的要求。", False),
    ("查询公司规定；撤销公司规定本轮不查询的要求。", True),
    ("查询公司规定；撤销本轮不查询公司规定的要求。", True),
    ("查询公司规定；取消查询公司规定的限制。", True),
    ("查询公司规定；取消查询公司规定的要求；现在查询公司规定。", True),
    ("查询公司规定；取消查询公司规定的要求；不要查询公司规定。", False),
    ("查询公司规定；取消查询公司手册的要求。", True),
    ("查询公司规定；查询公司手册；撤销查询公司规定的要求。", True),
    ("查询公司规定；查询公司手册；撤销查询公司规定的要求；撤销查询公司手册的任务。", False),
    (
        "不查询公司规定；查询公司手册；撤销查询公司规定的要求；查询公司规定；不要查询公司手册。",
        False,
    ),
    ("不查询公司规定；取消不查询公司规定的要求；查询公司规定。", True),
    ("不查询『公司规定』；取消不查询公司规定的要求；查询公司规定。", True),
    ("不查询公司规定；取消查询『公司规定』的限制；查询公司规定。", True),
    ("不查询公司规定；取消查询公司手册的限制；查询公司规定。", False),
    ("查询公司规定；不查询知识库；取消禁止查询知识库的要求。", False),
    ("查询公司规定；撤回这条查询要求。", False),
    ("查询公司规定；撤回该要求。", False),
    ("查询公司规定；查询公司手册；撤回这条查询要求。", True),
    ("查询公司规定；撤回这条查询要求；撤销查询公司规定的要求。", False),
    ("不查询知识库；撤回这条禁止查询的要求；查询公司规定。", True),
    ("查询公司规定；请不要撤回该查询请求。", True),
    ("查询公司规定；不要下载报告；撤销该要求。", True),
    ("查询公司规定；导出报告；撤销该要求。", True),
    ("查询公司规定；引用：“撤销查询公司规定的要求”。", True),
    ("查询公司规定；请解释撤销查询公司规定要求的原因。", True),
    ("本轮只做画像；请说明为什么撤销查询公司规定的要求。", False),
    ("本轮只做画像；如果撤销查询公司规定的要求，就只做画像。", False),
    ("查询公司规定；如果撤销查询公司规定的要求，就只做画像。", True),
    ("本轮只做画像；引用：『现在查询公司规定』。", False),
    ("本轮只做画像。\n\n> 请查询公司规定；撤销画像要求。", False),
    ("本轮只做画像。\n\n```text\n请查询公司规定。\n```", False),
    ("不查询知识库；查询“这条限制取消”；查询公司规定。", False),
    ("查询“撤销查询公司规定的要求”；不要查询公司规定。", True),
    ("查询公司规定；公司规定请本轮勿查询。", False),
    ("查询公司规定；本轮『公司规定』不查询。", False),
    ("查询公司规定；『公司规定』本轮不查询。", False),
    ("查询公司规定；公司规定的营收定义不查询。", True),
]


@pytest.mark.parametrize("goal,expected", CASES)
def test_withdrawal_direction_order_and_data_boundaries(goal, expected, tmp_path):
    assert_search(goal, expected, tmp_path)


HOSTS = [
    ("查询『公司规定』；本轮“公司规定”不检索。", False),
    ("查询“公司规定”；公司手册本轮不查询。", True),
    ("查询公司规定；撤回对公司规定的查询要求。", False),
    ("查询公司规定；查询公司手册；撤销查询公司规定的任务。", True),
    ("查询公司规定；撤销本轮不查询公司规定的要求。", True),
    ("查询公司规定；撤回该查询请求。", False),
    ("查询公司规定；请勿撤回该查询请求。", True),
    ("不查询公司规定；取消不查询“公司规定”的要求；查询公司规定。", True),
    ("本轮只做画像；请说明为什么撤销查询公司规定的要求。", False),
    ("查询公司规定；引用：『撤回查询公司规定的要求』。", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,required", HOSTS)
async def test_scoped_objects_and_withdrawals_in_host(tmp_path, suffix, required):
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
        ("不查询知识库；撤回该查询请求；查询公司规定。", False),
        ("不查询知识库；撤回该查询要求；查询公司规定。", False),
        ("不查询知识库；撤回该查询任务；查询公司规定。", False),
        ("查询公司规定；取消该限制。", True),
        ("不查询公司规定；取消该要求；查询公司规定。", True),
        ("查询公司规定；撤回这条禁止查询的要求。", True),
        ("不查询公司规定；改为查询公司规定；撤回该查询要求；查询公司规定。", False),
        ("查询公司规定；不查询公司手册；撤回该查询要求；查询公司手册；不要查询公司规定。", False),
    ],
)
def test_typed_reference_does_not_reverse_the_antecedent(goal, expected, tmp_path):
    assert_search(goal, expected, tmp_path)
