"""R2 contracts use actual profile/report/Host facts, not wording snapshots."""

import copy
import hashlib
from dataclasses import replace

import pandas as pd
import pytest
from apps.orchestrator.control.duplicate_repair import (
    capture_duplicate_obligations,
    resolve_duplicate_obligations,
    unresolved_duplicate_obligations,
)
from packages.orchestration.contracts import SuccessCriterion
from packages.session.task_store import TaskStore

from tests.test_manual02_identity_tokens import REF, validate
from tests.test_manual02_independent_regressions import contract, numeric, run_host
from tests.test_manual02_profile_contract import tool_profile
from tests.test_profile_subject_boundaries import record

FIRST = "设备ID有2条重复。存在2条完全重复的行记录。"
WITHDRAW = "行数5。撤回全部重复计数结论。"
OPTIONAL = "请提供设备数据行数，仅做画像。"


def frame():
    return pd.DataFrame({"设备ID": ["s1", "s1", "s1", "q9", "q8"], "温度": [7, 7, 7, 2, 3]})


@pytest.mark.parametrize(
    "goal,required",
    [
        ("请不要遗漏整行重复多余记录数。", {"整行"}),
        ("请不要跳过设备ID重复多余记录数。", {"设备ID"}),
        ("请在后续分析前先给出整行重复多余记录数。", {"整行"}),
        ("请别漏掉设备ID重复统计。", {"设备ID"}),
        ("请不要在报告中忽略整行重复计数。", {"整行"}),
        ("不要不统计整行重复。", {"整行"}),
        ("后续分析前，必须先提供设备ID重复多余记录数。", {"设备ID"}),
        ("不需要统计设备ID重复，但请给出整行重复多余记录数。", {"整行"}),
        ("整行重复无需统计，设备ID重复多余记录数必须提供。", {"设备ID"}),
        ("请只给出行数，不需要统计重复。", set()),
        ("请只给出行数，重复记录以后再处理。", set()),
        ("请做画像，后续处理重复记录。", set()),
        ("请下一轮再统计设备ID重复。", set()),
        ("请暂不统计整行重复。", set()),
        ("设备ID重复计数可以不提供。", set()),
    ],
)
def test_current_statistic_requirement_has_its_own_predicate(goal, required):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations([], numeric(FIRST, ev), ev, contract(goal))
    assert {o["subject"] for o in obs if o["required"]} == required
    changed = resolve_duplicate_obligations(obs, numeric(WITHDRAW, ev), WITHDRAW)
    assert unresolved_duplicate_obligations(changed) is bool(required)


def test_required_criterion_survives_goal_exclusion():
    c = replace(
        contract("重复统计以后再处理。"),
        success_criteria=(
            SuccessCriterion("required-row", "semantic", "不要遗漏整行重复多余记录数"),
        ),
    )
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations([], numeric(FIRST, ev), ev, c)
    assert {o["subject"] for o in obs if o["required"]} == {"整行"}


@pytest.mark.parametrize(
    "text,allowed",
    [
        ("撤回全部重复计数结论。", True),
        ("``撤回全部重复计数结论。``", False),
        ("````text\n```text\n撤回全部重复计数结论。\n```\n````", False),
        ("例如：\n撤回全部重复计数结论。", False),
        ("- 我现明确撤回全部重复计数结论。", True),
        ("示例：\n```text\n撤回全部重复计数结论。\n```", False),
        ("~~~text\n撤回全部重复计数结论。\n~~~", False),
        ("    撤回全部重复计数结论。", False),
        ("> 撤回全部重复计数结论。", False),
        ("“撤回全部重复计数结论。\n”", False),
        ("如果采用这一方案：\n\n撤回全部重复计数结论。", False),
        ("撤回全部重复计数结论，仅为示例。", False),
        ("示例：\n```text\n撤回全部重复计数结论。\n```\n我现明确撤回全部重复计数结论。", True),
        ("撤回全部重复计数结论。我尚未撤回上述结论。", False),
        ("我尚未撤回上述结论。我现在撤回全部重复计数结论。", True),
        ("撤回全部重复计数结论。引用：‘我尚未撤回上述结论。’", True),
    ],
)
def test_withdrawal_is_actual_present_speech(text, allowed):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations([], numeric(FIRST, ev), ev, contract(OPTIONAL))
    changed = resolve_duplicate_obligations(obs, numeric(text, ev), text)
    assert (not unresolved_duplicate_obligations(changed)) is allowed
    if allowed:
        for o in changed:
            r = o["resolution"]
            assert r["statement_sha256"] == hashlib.sha256(r["statement"].encode()).hexdigest()


def test_named_withdrawal_selects_one_fact_or_explicitly_all():
    profile = tool_profile(frame())
    first = record(profile, ref="d" * 32, version="a" * 64)
    second = record(profile, "other", ref="e" * 32, version="b" * 64)
    obs = []
    for ev in [first, second]:
        obs = capture_duplicate_obligations(
            obs, numeric("设备ID有2条重复。", [ev]), [ev], contract(OPTIONAL)
        )
    assert len(obs) == 2
    before = copy.deepcopy(obs)
    assert all(
        o["status"] == "pending"
        for o in resolve_duplicate_obligations(obs, [], "撤回设备ID重复计数结论。")
    )
    qualified = f"dataset_ref {'d' * 32}，dataset_version {'a' * 64}，撤回设备ID重复计数结论。"
    changed = resolve_duplicate_obligations(obs, [], qualified)
    assert [o["status"] for o in changed] == ["withdrawn_optional", "pending"]
    assert all(
        o["status"] == "withdrawn_optional"
        for o in resolve_duplicate_obligations(obs, [], "撤回全部设备ID重复计数结论。")
    )
    swapped = f"dataset_ref {'a' * 64}，dataset_version {'d' * 32}，撤回全部重复计数结论。"
    assert all(o["status"] == "pending" for o in resolve_duplicate_obligations(obs, [], swapped))
    multiline = f"dataset_ref {'d' * 32}。\ndataset_version {'a' * 64}。\n撤回全部重复计数结论。"
    assert [o["status"] for o in resolve_duplicate_obligations(obs, [], multiline)] == [
        "withdrawn_optional",
        "pending",
    ]
    denied = "撤回全部重复计数结论。我尚未撤回设备ID重复计数结论。"
    assert all(o["status"] == "pending" for o in resolve_duplicate_obligations(obs, [], denied))
    assert obs == before


@pytest.mark.parametrize("version", ["bc" * 32, "7d" * 32, "a" * 64])
@pytest.mark.parametrize("label", ["版本", "dataset_version"])
def test_typed_identity_alias_and_value_position_are_distinct(version, label):
    validate(
        f"dataset_ref {REF}，{label} {version}，整行重复多余记录数为1。", True, version=version
    )
    for text in [
        f"整行重复多余记录数为{label} {version}。",
        f"整行重复多余记录数 {version}。",
        f"整行重复多余记录数为\n{label} {version}。",
        f"{label} {version}，整行重复多余记录数为版本 {version}。",
        f"{label} {version}，整行重复多余记录数为9。",
    ]:
        outcome = validate(text, False, version=version)
        assert outcome["refs"] and any(not r["supported"] for r in outcome["refs"])


@pytest.mark.parametrize("value", ["abc123", "alphabetic", "v2.1", "v" + "b1" * 32])
@pytest.mark.parametrize("qualifier", ["版本 ", "标识符 ", "（版本 ", "dataset_version "])
def test_assignment_value_cannot_disappear_behind_an_identifier_label(value, qualifier):
    text = (
        "整行重复多余记录数为"
        + qualifier
        + value
        + ("）" if qualifier.startswith("（") else "")
        + "。"
    )
    outcome = validate(text, False)
    assert any(r.get("reason") == "unrecognized_numeric_expression" for r in outcome["refs"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal,first,second,expected",
    [
        ("请不要遗漏整行重复多余记录数。", FIRST, WITHDRAW, False),
        ("请在后续分析前先给出设备ID重复多余记录数。", FIRST, WITHDRAW, False),
        (OPTIONAL, FIRST, "示例：\n~~~\n撤回全部重复计数结论。\n~~~", False),
        (
            OPTIONAL,
            FIRST,
            "示例：\n```\n撤回全部重复计数结论。\n```\n我现明确撤回全部重复计数结论。",
            True,
        ),
        (
            "核验整行重复多余记录数。",
            "存在2条完全重复的行记录。",
            "dataset_ref {ref}，dataset_version {version}，整行重复多余记录数为2。",
            True,
        ),
        (
            "核验整行重复多余记录数。",
            "整行重复多余记录数为版本 abc123。",
            "整行重复多余记录数为版本 abc123。",
            False,
        ),
    ],
)
async def test_actual_host_preserves_requirement_action_and_identity(
    tmp_path, goal, first, second, expected
):
    w = await run_host(tmp_path, goal, first, second, frame().to_csv(index=False).encode())
    assert (w["run"]["status"] == "completed") is expected
    assert (w["report"]["status"] == "passed") is expected
    assert w["fixture_calls"] == 3
    assert (
        TaskStore(str(tmp_path / "host.db")).get_run(w["run"]["run_id"]).status
        == w["run"]["status"]
    )


def test_generic_type_noun_is_not_a_value_but_explicit_assignment_is():
    validate("该列是浮点型数值（metric 角色）。", True)
    validate("数值为alphabetic。", False)
    validate("整行重复多余记录数 alphabetic。", False)
