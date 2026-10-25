"""Complete document identities and explicit member paths remain distinct."""

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


def removal(target, action):
    if action == "ban":
        return f"本轮请勿查询{target}。"
    return f"撤回查询{target}的要求。"


@pytest.mark.parametrize("quotes", [("《", "》"), ("“", "”"), ("『", "』"), ('"', '"')])
@pytest.mark.parametrize("connector", ["的", "中的", "中"])
@pytest.mark.parametrize("action", ["ban", "withdraw"])
@pytest.mark.parametrize("literal", [True, False])
def test_literal_extension_and_external_member_are_distinct(
    quotes, connector, action, literal, tmp_path
):
    left, right = quotes
    requested = (
        f"{left}公司规定{connector}修订记录{right}"
        if literal
        else f"{left}公司规定{right}{connector}修订记录"
    )
    assert_search(f"请查询{requested}；" + removal("“公司规定”", action), literal, tmp_path)


@pytest.mark.parametrize("quotes", [("《", "》"), ("“", "”"), ("『", "』"), ('"', '"')])
@pytest.mark.parametrize("connector", ["的", "中的", "中"])
@pytest.mark.parametrize("action", ["ban", "withdraw"])
def test_same_complete_title_still_matches_across_delimiters(quotes, connector, action, tmp_path):
    left, right = quotes
    name = f"公司规定{connector}修订记录"
    assert_search(f"请查询{left}{name}{right}；" + removal(f"‘{name}’", action), False, tmp_path)


CASES = [
    ("查询《公司规定》；不要查询《公司规定的修订记录》。", True),
    ("查询《公司规定》；撤回查询《公司规定的修订记录》的请求。", True),
    ("查询《公司规定修订版》；不要查询《公司规定》。", True),
    ("查询公司规定修订版；不要查询公司规定。", True),
    ("查询公司规定中国版；不要查询公司规定。", True),
    ("查询《公司规定中国版》；不要查询《公司规定》。", True),
    ("查询《公司规定的修订记录》；撤回查询《公司规定》的修订记录的要求。", True),
    ("查询《公司规定》的修订记录；撤回查询《公司规定的修订记录》的要求。", True),
    ("查询《公司规定》的《修订记录的附件》；不要查询《公司规定》的《修订记录》。", True),
    ("查询《公司规定》的《修订记录》的附件；不要查询《公司规定》的《修订记录》。", False),
    ("查询《公司规定》的《修订记录》；不要查询《公司规定》的《修订记录的附件》。", True),
    ("查询《公司规定》的《修订记录》；撤回查询“公司规定”中的“修订记录”的要求。", False),
    ("查询《公司规定》的《修订记录》；不要查询《公司规定》的《营收定义》。", True),
    ("查询《公司规定的修订记录》；查询《公司规定》；不要查询《公司规定》。", True),
    ("查询《公司规定的修订记录》；查询《公司规定》；撤回查询《公司规定的修订记录》的请求。", True),
    (
        "查询《公司规定的修订记录》；查询《公司规定》；撤回查询《公司规定》的请求；撤回查询《公司规定的修订记录》的请求。",
        False,
    ),
    ("查询《公司规定》；改为查询《公司规定的修订记录》；不要查询《公司规定》。", True),
    ("查询《公司规定的修订记录》；改为查询《公司规定》；不要查询《公司规定》。", False),
    (
        "不要查询《公司规定的修订记录》；撤回不查询《公司规定》的要求；查询《公司规定的修订记录》。",
        False,
    ),
    ("不要查询《公司规定》；撤回不查询《公司规定的修订记录》的要求；查询《公司规定》。", False),
    ("不要查询《公司规定》；查询《公司规定的修订记录》。", True),
    ("查询《公司规定的修订记录》；引用：“撤回查询《公司规定的修订记录》的请求”。", True),
    ("查询《公司规定的修订记录》；请勿撤回查询《公司规定的修订记录》的请求。", True),
    ("查询《公司规定的修订记录》；现在请勿查询知识库。", False),
]


@pytest.mark.parametrize("goal,expected", CASES)
def test_document_identity_and_member_scope_controls(goal, expected, tmp_path):
    assert_search(goal, expected, tmp_path)


HOSTS = [
    ("查询《公司规定的修订记录》；不要查询《公司规定》。", True),
    ("查询《公司规定的修订记录》；撤回查询《公司规定》的要求。", True),
    ("查询《公司规定》的修订记录；不要查询《公司规定》。", False),
    ("查询《公司规定》的修订记录；撤回查询《公司规定》的请求。", False),
    ("查询《公司规定的修订记录》；撤回查询“公司规定的修订记录”的要求。", False),
    ("查询《公司规定》的修订记录；不要查询《公司规定的修订记录》。", True),
    ("查询《公司规定的修订记录》；查询《公司规定》；撤回查询《公司规定》的请求。", True),
    (
        "查询《公司规定的修订记录》；查询《公司规定》；撤回查询《公司规定》的请求；撤回查询《公司规定的修订记录》的请求。",
        False,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,required", HOSTS)
async def test_complete_title_obligations_in_host(tmp_path, suffix, required):
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
    assert not any(e["source"].get("tool") == "knowledge_search" for e in w["evidence"])
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
