"""R4 ownership, matched speech boundaries and temporal operands through real tools."""

from dataclasses import replace

import pytest
from apps.orchestrator.control.duplicate_repair import (
    capture_duplicate_obligations,
    resolve_duplicate_obligations,
    unresolved_duplicate_obligations,
)
from packages.orchestration.contracts import SuccessCriterion
from packages.session.task_store import TaskStore

from tests.test_manual02_independent_regressions import contract, numeric, run_host
from tests.test_manual02_profile_contract import tool_profile
from tests.test_manual02_r3_contract import evaluate, frame
from tests.test_profile_subject_boundaries import record


@pytest.mark.parametrize(
    "text,expected",
    [
        ("整行重复多余记录数为未知（当前行数为5）。", False),
        ("整行重复多余记录数为未知（当前行数5）。", False),
        ("整行重复多余记录数为未知（温度最大值为7）。", False),
        ("整行重复多余记录数为α（行数为5）。", False),
        ("整行重复多余记录数为🧬（行数为5）。", False),
        ("整行重复多余记录数为（行数为5）。", False),
        ("整行重复多余记录数为（5行）。", False),
        ("整行重复多余记录数为（2列）。", False),
        ("设备ID重复多余记录数为未知（整行重复多余记录数为2）。", False),
        ("整行重复多余记录数为未知（设备ID重复多余记录数为2）。", False),
        ("行数为未知（温度最大值为7）。", False),
        ("行数为未知（列数为2）。", False),
        ("|整行重复多余记录数|\n|---|\n|未知（行数为5）|", False),
        ("|整行重复多余记录数|\n|---|\n|（2列）|", False),
        ("|整行重复多余记录数|\n|---|\n|2|", True),
        ("整行重复多余记录数为2。行数为5。", True),
        ("设备ID重复多余记录数为2。", True),
        ("整行重复多余记录数为2（重复率为40%）。", True),
        ("温度最大值为7。", True),
        ("本次说明数据质量口径，尚未提供统计值。", True),
    ],
)
def test_each_statistic_value_has_its_own_typed_evidence(text, expected):
    result = evaluate(text, expected)
    if expected:
        for claim in result["claims"]:
            for ref in claim["value_refs"]:
                if "statistic_slot_span" in ref:
                    a, b = ref["statement_span"]
                    assert claim["statement"][a:b] == ref["token"]
    elif "未知" in text or "α" in text or "🧬" in text:
        assert any(ref.get("reason") == "unparsed_statistic_value" for ref in result["refs"])


@pytest.mark.parametrize(
    "text,allowed",
    [
        ("如果采用以下模板：\n说明：“质量画像”。\n我现在明确撤回全部重复计数结论。", False),
        ("如果采用以下模板：\n说明：`质量画像`。\n我现在明确撤回全部重复计数结论。", False),
        ("如果采用以下模板：\n说明：『质量画像』。\n我现在明确撤回全部重复计数结论。", False),
        ("如果采用以下模板：\n示例：“质量画像”。\n我现在明确撤回全部重复计数结论。", False),
        (
            "如果采用以下模板：\n说明：\n~~~text\n质量画像\n~~~\n我现在明确撤回全部重复计数结论。",
            False,
        ),
        ("如果采用以下模板：\n说明：\n> 质量画像\n\n我现在明确撤回全部重复计数结论。", False),
        ("如果采用以下模板：\n说明：“质量画像”。\n\n我现在明确撤回全部重复计数结论。", False),
        ("示例：“我现在明确撤回全部重复计数结论。”\n我现在明确撤回全部重复计数结论。", True),
        (
            "示例：『标题“质量画像”。我现在明确撤回全部重复计数结论。』\n我现在明确撤回全部重复计数结论。",
            True,
        ),
        (
            "示例：\n~~~text\n说明：“质量画像”。\n我现在明确撤回全部重复计数结论。\n~~~\n我现在明确撤回全部重复计数结论。",
            True,
        ),
        ("我现在明确撤回全部重复计数结论。引用：“我并未撤回上述结论。”", True),
        ("我现在明确撤回全部重复计数结论。以上仅为模板。", False),
    ],
)
def test_only_the_introduced_boundary_releases_its_own_speech(text, allowed):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations(
        [], numeric("设备ID有2条重复。", ev), ev, contract("只做画像。")
    )
    changed = resolve_duplicate_obligations(obs, [], text)
    assert (not unresolved_duplicate_obligations(changed)) is allowed
    assert all(o["status"] == "pending" for o in obs)


@pytest.mark.parametrize(
    "goal,required",
    [
        ("后续处理整行重复之前，请先给出设备ID重复多余记录数。", {"设备ID"}),
        ("在后续处理整行重复之前，请先给出设备ID重复多余记录数。", {"设备ID"}),
        ("请先给出设备ID重复多余记录数，在后续处理整行重复之前。", {"设备ID"}),
        ("处理整行重复之前，请先给出设备ID重复多余记录数。", {"设备ID"}),
        ("在处理整行重复之前，请先给出设备ID重复多余记录数。", {"设备ID"}),
        ("整行重复后续处理之前，请先给出设备ID重复多余记录数。", {"设备ID"}),
        ("后续处理设备ID重复之前，请先给出整行重复多余记录数。", {"整行"}),
        ("后续处理整行重复之前，请先给出整行重复多余记录数。", {"整行"}),
        (
            "后续处理整行重复之前，请先给出设备ID重复多余记录数，并给出整行重复多余记录数。",
            {"设备ID", "整行"},
        ),
        ("现在给出整行重复多余记录数之前，请先给出设备ID重复多余记录数。", {"设备ID", "整行"}),
        ("后续处理整行重复之前。请先给出设备ID重复多余记录数。", {"设备ID", "整行"}),
        ("请不要遗漏整行重复多余记录数，设备ID重复可以以后处理。", {"整行"}),
    ],
)
def test_temporal_operand_and_current_delivery_are_separate(goal, required):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations(
        [], numeric("设备ID有2条重复。存在2条完全重复的行记录。", ev), ev, contract(goal)
    )
    assert {o["subject"] for o in obs if o["required"]} == required


def test_required_contract_still_overrides_a_deferred_goal_operand():
    ev = [record(tool_profile(frame()))]
    c = replace(
        contract("后续处理整行重复之前，请先给出设备ID重复多余记录数。"),
        success_criteria=(SuccessCriterion("dup", "semantic", "核验整行重复多余记录数"),),
    )
    obs = capture_duplicate_obligations(
        [], numeric("设备ID有2条重复。存在2条完全重复的行记录。", ev), ev, c
    )
    assert {o["subject"] for o in obs if o["required"]} == {"设备ID", "整行"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal,second,expected,status",
    [
        ("请给出整行重复多余记录数。", "整行重复多余记录数为2。", True, "resolved"),
        ("请给出整行重复多余记录数。", "行数5。", False, "pending"),
        (
            "请给出整行重复多余记录数。",
            "我现在明确撤回整行重复计数结论。",
            False,
            "unfulfilled_required",
        ),
        ("只提供行数。", "行数5。我现在明确撤回整行重复计数结论。", True, "withdrawn_optional"),
    ],
)
async def test_actual_host_nested_unknown_creates_persistent_obligation(
    tmp_path, goal, second, expected, status
):
    w = await run_host(
        tmp_path,
        goal,
        "整行重复多余记录数为未知（当前行数为5）。",
        second,
        frame().to_csv(index=False).encode(),
    )
    assert w["fixture_calls"] == 3
    assert (w["run"]["status"] == "completed") is expected
    assert (w["report"]["status"] == "passed") is expected
    assert len(w["obligations"]) == 1 and w["obligations"][0]["status"] == status
    assert (
        TaskStore(str(tmp_path / "host.db")).get_run(w["run"]["run_id"]).status
        == w["run"]["status"]
    )
