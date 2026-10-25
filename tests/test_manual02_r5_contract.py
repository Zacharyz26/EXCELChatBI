"""R5: ordered deliveries remain obligations; deferral is bound to its own operand."""

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
from tests.test_manual02_r3_contract import frame
from tests.test_profile_subject_boundaries import record

FIRST = "设备ID有2条重复。存在2条完全重复的行记录。"
A = "整行重复多余记录数"
B = "设备ID重复多余记录数"
BOTH = {"整行", "设备ID"}


@pytest.mark.parametrize(
    "goal,required",
    [
        (f"给出{A}之后，请给出{B}。", BOTH),
        (f"提供{B}以后，再提供{A}。", BOTH),
        (f"给出{A}之前，请先给出{B}。", BOTH),
        (f"在提供{B}以前，请先提供{A}。", BOTH),
        (f"后续给出{A}之后，请给出{B}。", BOTH),
        (f"下一步提供{B}以后，再提供{A}。", BOTH),
        (f"后续给出{A}之前，请先给出{B}。", BOTH),
        (f"计算{A}之后，再给出{B}。", BOTH),
        (f"统计{A}之前，请先给出{B}。", BOTH),
        (f"下一步计算{A}之后，再提供{B}。", BOTH),
        (f"先给出{A}，再给出{B}，最后核验{A}。", BOTH),
        (f"请给出{A}之后再提供{B}。", BOTH),
        (f"给出{A}之后；再给出{B}。", BOTH),
        (f"给出{A}之后，\n请给出{B}。", BOTH),
        (f"后续处理整行重复之前，请先给出{B}。", {"设备ID"}),
        (f"处理整行重复之前，请先给出{B}。", {"设备ID"}),
        (f"如果后续处理整行重复之后，请给出{B}。", {"设备ID"}),
        (f"若下一轮计算{A}之后，请先给出{B}。", {"设备ID"}),
        (f"等后续处理设备ID重复以后，请给出{A}。", {"整行"}),
        (f"整行重复统计本次可以留待下一轮处理之后，请给出{B}。", {"设备ID"}),
        (f"本次无需提供{A}，请给出{B}。", {"设备ID"}),
        (f"请给出{B}，整行重复统计本次无需提供。", {"设备ID"}),
        (f"请不要遗漏{A}，再给出{B}。", BOTH),
        (f"请不要暂时忽略{A}，再给出{B}。", BOTH),
        (f"本次不用给出{A}，之后请给出{B}。", {"设备ID"}),
        (f"设备ID重复统计以后处理，但现在必须给出{B}和{A}。", BOTH),
        (f"如果后续处理整行重复之前，请给出{A}和{B}。", BOTH),
        (f"后续处理整行重复之后，请给出{B}。", BOTH),
    ],
)
def test_sequence_direction_never_silently_waives_a_delivery(goal, required):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations([], numeric(FIRST, ev), ev, contract(goal))
    assert {o["subject"] for o in obs if o["required"]} == required
    after = resolve_duplicate_obligations(obs, [], "我现在明确撤回全部重复计数结论。")
    assert {o["subject"] for o in after if o["status"] == "unfulfilled_required"} == required
    assert unresolved_duplicate_obligations(after) is bool(required)
    assert all(o["status"] == "pending" for o in obs)


def multi_frame():
    return frame().assign(工单ID=["x1", "x1", "x1", "x2", "x3"])


@pytest.mark.parametrize(
    "goal,required",
    [
        (f"给出{A}之后，给出{B}之后，再给出工单ID重复多余记录数。", {"整行", "设备ID", "工单ID"}),
        (f"给出工单ID重复多余记录数以后，提供{A}以后，再给出{B}。", {"整行", "设备ID", "工单ID"}),
        (f"先提供{B}，再提供工单ID重复多余记录数，最后提供{A}。", {"整行", "设备ID", "工单ID"}),
        (
            f"后续处理整行重复之前，先给出{B}之后，再给出工单ID重复多余记录数。",
            {"设备ID", "工单ID"},
        ),
        (f"本次无需给出{A}，给出{B}以后，再给出工单ID重复多余记录数。", {"设备ID", "工单ID"}),
        (f"请不要遗漏{A}，工单ID重复统计下一轮处理，再给出{B}。", {"整行", "设备ID"}),
    ],
)
def test_every_operand_in_a_three_task_sequence_is_accounted_for(goal, required):
    data = multi_frame()
    assert int(data.duplicated().sum()) == 2
    ev = [record(tool_profile(data))]
    obs = capture_duplicate_obligations(
        [], numeric(FIRST + "工单ID有2条重复。", ev), ev, contract(goal)
    )
    assert {o["subject"] for o in obs} == {"整行", "设备ID", "工单ID"}
    assert {o["subject"] for o in obs if o["required"]} == required


def test_required_criterion_overrides_explicit_goal_deferral():
    ev = [record(tool_profile(frame()))]
    c = replace(
        contract(f"本次无需给出{A}，请给出{B}。"),
        success_criteria=(SuccessCriterion("whole", "semantic", f"核验{A}"),),
    )
    obs = capture_duplicate_obligations([], numeric(FIRST, ev), ev, c)
    assert {o["subject"] for o in obs if o["required"]} == BOTH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal,second,expected,required",
    [
        (f"给出{A}之后，请给出{B}。", f"{B}为2。我现在明确撤回整行重复计数结论。", False, BOTH),
        (f"提供{B}以后，再给出{A}。", f"{A}为2。我现在明确撤回设备ID重复计数结论。", False, BOTH),
        (f"给出{A}之后，请给出{B}。", f"{A}为2。{B}为2。", True, BOTH),
        (f"后续给出{A}之后，请给出{B}。", f"{B}为2。我现在明确撤回整行重复计数结论。", False, BOTH),
        (
            f"如果后续处理整行重复之后，请给出{B}。",
            f"{B}为2。我现在明确撤回整行重复计数结论。",
            True,
            {"设备ID"},
        ),
        (
            f"整行重复统计本次留待下一轮处理，请给出{B}。",
            f"{B}为2。我现在明确撤回整行重复计数结论。",
            True,
            {"设备ID"},
        ),
    ],
)
async def test_actual_host_sequence_obligations_survive_withdrawal(
    tmp_path, goal, second, expected, required
):
    w = await run_host(tmp_path, goal, FIRST, second, frame().to_csv(index=False).encode())
    assert w["fixture_calls"] == 3
    assert {o["subject"] for o in w["obligations"] if o["required"]} == required
    assert (w["run"]["status"] == "completed") is expected
    assert (w["report"]["status"] == "passed") is expected
    assert (
        TaskStore(str(tmp_path / "host.db")).get_run(w["run"]["run_id"]).status
        == w["run"]["status"]
    )
