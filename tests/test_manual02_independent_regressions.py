"""Independent P1 regressions: auditable withdrawal and immutable duplicate scope."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.duplicate_repair import (
    capture_duplicate_obligations,
    resolve_duplicate_obligations,
    restored_duplicate_obligations,
    unresolved_duplicate_obligations,
)
from apps.orchestrator.control.report_claims import validate_report_narratives
from fastapi.testclient import TestClient
from packages.common.dataset_store import load_dataframe
from packages.models.types import ModelResponse, ToolCall
from packages.orchestration.contracts import SuccessCriterion
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_manual02_profile_contract import CAPTURE, EXPLICIT_ANSWER, evaluate, tool_profile
from tests.test_profile_subject_boundaries import check, record

OMITTED = "\n".join(
    line
    for line in EXPLICIT_ANSWER.splitlines()
    if not line.startswith(("- 记录ID重复", "- 整行重复"))
)
WITHDRAWN = OMITTED + "\n撤回全部重复计数结论。"


def contract(goal):
    return build_minimal_contract(
        run_id="r", user_text=goal, chart_required=False, report_required=False, pdf_required=False
    )


def numeric(text, evidence):
    return extract_numeric_claims(final_text=text, goal="复核画像", evidence=evidence)


@pytest.mark.parametrize(
    "goal,required",
    [
        (CAPTURE["goal"], set()),
        ("请给出重复统计。", {"记录ID", "整行"}),
        ("请给出记录ID重复多余记录数。", {"记录ID"}),
        ("请给出整行重复多余记录数。", {"整行"}),
        ("请做画像，不要给重复统计。", set()),
        ("请做画像，后续处理重复记录。", set()),
    ],
)
def test_required_and_optional_are_distinct(goal, required):
    ev = [record(CAPTURE["tool_result"])]
    obligations = capture_duplicate_obligations(
        [], numeric(CAPTURE["answer"], ev), ev, contract(goal)
    )
    assert {o["subject"] for o in obligations if o["required"]} == required
    assert {o["subject"] for o in obligations} == {"记录ID", "整行"}
    removed = resolve_duplicate_obligations(obligations, numeric(OMITTED, ev), OMITTED)
    assert all(o["status"] == "pending" for o in removed)
    withdrawn = resolve_duplicate_obligations(obligations, numeric(WITHDRAWN, ev), WITHDRAWN)
    assert {o["subject"] for o in withdrawn if o["status"] == "unfulfilled_required"} == required
    assert unresolved_duplicate_obligations(withdrawn) is bool(required)
    assert all(o["resolution"]["kind"] == "explicit_withdrawal" for o in withdrawn)
    # Original evidence is retained, never replaced by the model's later prose.
    assert obligations == capture_duplicate_obligations(
        obligations, numeric(CAPTURE["answer"], ev), ev, contract(goal)
    )


def test_required_contract_criterion_and_same_value_wrong_version():
    ev = [record(CAPTURE["tool_result"])]
    c = replace(
        contract(CAPTURE["goal"]),
        success_criteria=(SuccessCriterion("dup", "semantic", "核验整行重复多余记录数"),),
    )
    obligations = capture_duplicate_obligations([], numeric(CAPTURE["answer"], ev), ev, c)
    assert {o["subject"] for o in obligations if o["required"]} == {"整行"}
    current = resolve_duplicate_obligations(
        obligations, numeric(EXPLICIT_ANSWER, ev), EXPLICIT_ANSWER
    )
    assert not unresolved_duplicate_obligations(current)
    for altered in [
        record(CAPTURE["tool_result"], version="b" * 64),
        record(CAPTURE["tool_result"], ref="e" * 32),
    ]:
        wrong = resolve_duplicate_obligations(
            obligations, numeric(EXPLICIT_ANSWER, [altered]), EXPLICIT_ANSWER
        )
        assert unresolved_duplicate_obligations(wrong)
    # A previous resolution does not let the next candidate silently remove it.
    assert unresolved_duplicate_obligations(resolve_duplicate_obligations(current, [], OMITTED))


@pytest.mark.parametrize(
    "text",
    [
        "没有撤回全部重复计数结论。",
        "请撤回全部重复计数结论。",
        "若需要，撤回全部重复计数结论。",
        "撤回未知字段重复计数结论。",
    ],
)
def test_withdrawal_is_explicit_not_quoted_or_conditional(text):
    ev = [record(CAPTURE["tool_result"])]
    obs = capture_duplicate_obligations(
        [], numeric(CAPTURE["answer"], ev), ev, contract(CAPTURE["goal"])
    )
    assert unresolved_duplicate_obligations(resolve_duplicate_obligations(obs, [], text))


@pytest.mark.parametrize(
    "goal,text,expected",
    [
        (CAPTURE["goal"], OMITTED, False),
        (CAPTURE["goal"], WITHDRAWN, True),
        ("请核验重复统计。", WITHDRAWN, False),
        ("请核验重复统计。", EXPLICIT_ANSWER, True),
        (
            "请核验整行重复多余记录数。",
            OMITTED + "\n整行重复多余记录数为1。\n撤回记录ID重复计数结论。",
            True,
        ),
        (
            "请核验记录ID重复多余记录数。",
            OMITTED + "\n整行重复多余记录数为1。\n撤回记录ID重复计数结论。",
            False,
        ),
    ],
)
def test_report_revalidates_pending_delivery(goal, text, expected):
    ev = [record(CAPTURE["tool_result"])]
    obs = capture_duplicate_obligations([], numeric(CAPTURE["answer"], ev), ev, contract(goal))
    a = Artifact(
        "p",
        "c",
        "m",
        "profile",
        CAPTURE["tool_result"],
        None,
        "get_data_profile",
        {"dataset_ref": "d" * 32, "data_version_hash": "a" * 64},
        "d" * 32,
        "now",
    )
    if expected:
        assert (
            validate_report_narratives(
                [a], {"title": "画像", "insights": text}, repair_obligations=obs
            )["status"]
            == "passed"
        )
    else:
        with pytest.raises(ValueError, match="未解决的重复计数"):
            validate_report_narratives(
                [a], {"title": "画像", "insights": text}, repair_obligations=obs
            )


def device_frame(same_value=False):
    if same_value:
        return pd.DataFrame(
            {"设备ID": ["s1", "s1", "s1", "s2", "s3"], "振幅": [2.0, 2.0, 2.0, 5.0, 8.0]}
        )
    return pd.DataFrame(
        {
            "设备ID": ["s1", "s1", "s1", "s2", "s2", "s3", None, None, "s4"],
            "振幅": [2.0, 2.0, 2.0, 5.0, 5.0, 8.0, None, None, 8.0],
        }
    )


@pytest.mark.parametrize("same_value", [False, True])
@pytest.mark.parametrize(
    "prefix",
    [
        "设备ID为空的记录中，",
        "设备ID非空的记录中，",
        "设备ID为s1的记录中，",
        "设备ID为s1的记录中；",
        "未知设备的",
        "筛选后，",
        "过滤后的",
        "设备ID为空的记录中，顺带提示三个数据质量点，供后续分步处理时参考：",
        "设备ID为空的记录中，数据质量提示：设备ID为常量列，",
    ],
)
def test_complete_subset_scope_even_for_coincident_values(same_value, prefix):
    frame = device_frame(same_value)
    count = int(frame.duplicated().sum())
    subset = int(frame[frame["设备ID"] == "s1"].duplicated().sum())
    assert (count == subset) is same_value
    result = tool_profile(frame)
    evaluate(result, prefix + f"整行重复多余记录数为{count}。", False)
    evaluate(
        result,
        prefix + f"设备ID重复多余记录数为{int(frame.设备ID.dropna().duplicated().sum())}。",
        False,
    )


@pytest.mark.parametrize(
    "heading",
    [
        "",
        "全表",
        "整个数据集",
        "当前数据集",
        "本数据集",
        "数据集 " + "d" * 32 + "，",
        "版本 " + "a" * 64 + "，",
        "顺带提示三个数据质量点，供后续分步处理时参考：",
    ],
)
def test_whole_dataset_scope_controls(heading):
    result = tool_profile(device_frame())
    text = heading + "整行重复多余记录数为4。"
    assert check(text, [record(result)])["verification"]["passed"]


@pytest.mark.parametrize(
    "version,filters",
    [("b" * 64, []), ("a" * 64, [{"column": "设备ID", "op": "==", "value": "s1"}])],
)
def test_scope_identity_and_uninterpreted_filters_fail_closed(version, filters):
    ev = record(tool_profile(device_frame()), version=version)
    summary = copy.deepcopy(ev.summary)
    summary["numeric_context"]["filters"] = filters
    outcome = check(
        "版本 " + "a" * 64 + "，整行重复多余记录数为4。", [replace(ev, summary=summary)]
    )
    assert not outcome["verification"]["passed"]


async def run_host(tmp_path, goal, first, second, source, *, filtered=False):
    store = SessionStore(str(tmp_path / "host.db"))
    project = store.create_project("IR synthetic")
    conv = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        up = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conv.id},
            files={"file": ("synthetic.csv", source, "text/csv")},
        )
    assert up.status_code == 200
    ref = up.json()["dataset_ref"]
    registry = _registry(AgentContext(store, project.id, conv.id))
    if filtered:
        transformed = registry.execute(
            "transform_dataset",
            json.dumps(
                {"dataset_ref": ref, "filters": [{"column": "设备ID", "op": "==", "value": "s1"}]}
            ),
        )
        derived = transformed["dataset_ref"]
        assert derived != ref
        assert (
            len(load_dataframe(derived)) == 3
            and int(load_dataframe(derived).duplicated().sum()) == 2
        )
        ref = derived
        goal = f"数据集 {ref}，{goal}"
    version = ""

    class Gateway:
        calls = 0

        async def stream_turn(self, scenario, messages, **kwargs):
            nonlocal version, second
            self.calls += 1
            assert self.calls <= 3
            if self.calls > 1:
                latest = TaskStore(store.db_path).get_latest_run_for_conversation(conv.id)
                version = TaskStore(store.db_path).data_version_hash(latest.run_id)
                second = second.replace("{ref}", ref).replace("{version}", version)
            if self.calls == 1:
                yield ModelResponse(
                    content="",
                    model="offline-ir",
                    tool_calls=[
                        ToolCall("p", "get_data_profile", json.dumps({"dataset_ref": ref}))
                    ],
                )
                return
            yield ModelResponse(content=first if self.calls == 2 else second, model="offline-ir")

    gw = Gateway()
    try:
        events = [
            e
            async for e in stream_agent_chat(
                conversation_id=conv.id,
                project_id=project.id,
                store=store,
                user_text=goal,
                gateway=gw,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_model_rounds=4, max_tool_calls=12, max_parallel_tools=1),
            )
        ]
        task = TaskStore(store.db_path)
        run = task.get_latest_run_for_conversation(conv.id)
        persisted = task.list_events(run.run_id)
        obs = restored_duplicate_obligations(task.list_events_by_type(run.run_id, "verification"))
        profile = next(
            a for a in store.list_artifacts(conv.id) if a.source_tool == "get_data_profile"
        )
        try:
            report = registry.execute(
                "generate_report",
                json.dumps(
                    {
                        "title": "IR画像",
                        "analysis_ids": [profile.params["analysis_id"]],
                        "insights": second,
                        "include_pdf": False,
                    }
                ),
            )
            report = {
                "status": report["validation"]["status"],
                "content": Path(report["md_path"]).read_text(),
            }
        except AgentToolError as exc:
            report = {"status": "rejected", "error": str(exc)}
        witness = {
            "goal": goal,
            "original": first,
            "candidate": second,
            "run": asdict(run),
            "obligations": obs,
            "events": [asdict(e) for e in persisted],
            "sse_events": events,
            "evidence": [asdict(e) for e in task.list_evidence(run.run_id)],
            "report": report,
            "fixture_calls": gw.calls,
            "paid_calls": 0,
            "dataset_ref": ref,
            "version": version,
        }
        (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
        return witness
    finally:
        await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal,answer,expected",
    [
        (CAPTURE["goal"], OMITTED, False),
        (CAPTURE["goal"], WITHDRAWN, True),
        (CAPTURE["goal"], OMITTED + "\n撤回记录ID重复计数结论。\n撤回整行重复计数结论。", True),
        ("请核验重复统计。", OMITTED, False),
        ("请核验重复统计。", WITHDRAWN, False),
        ("请核验重复统计。", EXPLICIT_ANSWER, True),
        (
            "请核验整行重复多余记录数。",
            OMITTED + "\n整行重复多余记录数为1。\n撤回记录ID重复计数结论。",
            True,
        ),
    ],
)
async def test_actual_host_optional_withdrawal_vs_required_delivery(
    tmp_path, goal, answer, expected
):
    root = Path(__file__).resolve().parents[1]
    w = await run_host(
        tmp_path,
        goal,
        CAPTURE["answer"],
        answer,
        (root / "examples/handoff/broadcast.csv").read_bytes(),
    )
    assert (w["run"]["status"] == "completed") is expected, w
    assert (w["report"]["status"] == "passed") is expected, w
    assert w["fixture_calls"] == 3
    if "撤回" in answer:
        assert any(
            o.get("resolution", {}).get("kind") == "explicit_withdrawal"
            for o in w["obligations"]
            if isinstance(o.get("resolution"), dict)
        )
    if not expected:
        assert w["run"]["terminal_reason"] == "unsupported_numeric_claim"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_value", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
async def test_actual_host_derived_subset_and_parent_borrow(tmp_path, same_value, filtered):
    frame = device_frame(same_value)
    count = int(frame.duplicated().sum())
    initial = f"存在{2 if filtered else count}条完全重复的行记录。"
    candidate = (
        "数据集 {ref}，版本 {version}，整行重复多余记录数为2。"
        if filtered
        else f"设备ID为s1的记录中，整行重复多余记录数为{count}。"
    )
    w = await run_host(
        tmp_path,
        "复核整行重复多余记录数。",
        initial,
        candidate,
        frame.to_csv(index=False).encode(),
        filtered=filtered,
    )
    assert (w["run"]["status"] == "completed") is filtered, w
    assert (w["report"]["status"] == "passed") is filtered, w
    assert w["fixture_calls"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,answer,expected,subjects",
    [
        (
            "记录ID有1条重复，记录ID为空的记录中，整行重复多余记录数为1。",
            "记录ID重复多余记录数为1。整行重复多余记录数为1。",
            False,
            {"记录ID", "整行"},
        ),
        (
            "记录ID有1条重复，存在1条完全重复的行记录。",
            "整行重复多余记录数为1。",
            False,
            {"记录ID", "整行"},
        ),
        (
            "记录ID有1条重复，存在1条完全重复的行记录。",
            "记录ID重复多余记录数为1。整行重复多余记录数为1。",
            True,
            {"记录ID", "整行"},
        ),
        (
            "记录ID有1条重复，存在1条完全重复的行记录。",
            "整行重复多余记录数为1。撤回记录ID重复计数结论。",
            True,
            {"记录ID", "整行"},
        ),
        (
            "记录ID重复多余记录数为1，存在1条完全重复的行记录。",
            "整行重复多余记录数为1。",
            True,
            {"整行"},
        ),
        (
            "记录ID为空的记录中，记录ID重复多余记录数为1，整行重复多余记录数为1。",
            "记录ID重复多余记录数为1。整行重复多余记录数为1。",
            False,
            {"记录ID", "整行"},
        ),
    ],
)
async def test_actual_host_mixed_duplicate_assertions(
    tmp_path, initial, answer, expected, subjects
):
    root = Path(__file__).resolve().parents[1]
    witness = await run_host(
        tmp_path,
        CAPTURE["goal"],
        initial,
        answer,
        (root / "examples/handoff/broadcast.csv").read_bytes(),
    )
    assert (witness["run"]["status"] == "completed") is expected, witness
    assert (witness["report"]["status"] == "passed") is expected, witness
    assert {item["subject"] for item in witness["obligations"]} == subjects
    assert all(item["original_statement"] == initial for item in witness["obligations"])
    assert witness["fixture_calls"] == 3
