"""R3 contracts: typed value slots and scoped speech through actual tools/Host."""

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from apps.orchestrator.control.duplicate_repair import (
    capture_duplicate_obligations,
    resolve_duplicate_obligations,
    restored_duplicate_obligations,
    unresolved_duplicate_obligations,
)
from apps.orchestrator.control.report_claims import validate_report_narratives
from fastapi.testclient import TestClient
from packages.models.types import ModelResponse, ToolCall
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_manual02_independent_regressions import contract, numeric, run_host
from tests.test_manual02_profile_contract import tool_profile
from tests.test_profile_subject_boundaries import check, record


def frame():
    return pd.DataFrame(
        {"设备ID": ["s1", "s1", "s1", "q9", "q8"], "温度": [7.0, 7.0, 7.0, 2.0, 3.0]}
    )


def evaluate(text, expected, data=None):
    profile = tool_profile(frame() if data is None else data)
    outcome = check(text, [record(profile)])
    artifact = Artifact(
        "p",
        "c",
        "m",
        "profile",
        profile,
        None,
        "get_data_profile",
        {"dataset_ref": "d" * 32},
        "d" * 32,
        "now",
    )
    try:
        report = validate_report_narratives([artifact], {"title": "画像", "insights": text})
    except ValueError as exc:
        report = {"status": "rejected", "error": str(exc)}
    assert outcome["verification"]["passed"] is expected, outcome
    assert (report["status"] == "passed") is expected, report
    return outcome


@pytest.mark.parametrize("value", ["甲乙", "αβγ", "ａｂｃ", "ééé", "абв", "🧪", "未知", "", " \n "])
@pytest.mark.parametrize("prefix", ["整行重复多余记录数为", "行数5，设备ID重复多余记录数为"])
def test_unparsed_assignment_always_has_a_failed_value_slot(value, prefix):
    outcome = evaluate(prefix + value + "。", False)
    refs = [r for r in outcome["refs"] if r.get("reason") == "unparsed_statistic_value"]
    assert refs and all(
        r["statement_type"] == "statistic_assignment" and r["validation"] == "unparsed"
        for r in refs
    )
    for claim in outcome["claims"]:
        for ref in claim["value_refs"]:
            if ref.get("reason") == "unparsed_statistic_value":
                a, b = ref["statement_span"]
                assert claim["statement"][a:b] == ref["token"]


@pytest.mark.parametrize(
    "text",
    [
        "本段说明重复统计口径。",
        "后续可由工具计算重复多余记录数。",
        "设备ID是字符串（string 类型），温度是浮点型数值（metric 角色）。",
        "整行重复多余记录数为2。",
        "设备ID重复多余记录数为2。行数为5。",
    ],
)
def test_general_prose_and_verified_assignments_remain_valid(text):
    evaluate(text, True)


def test_known_field_is_not_an_unknown_version_declaration():
    evaluate("版本甲乙均值为2。", True, pd.DataFrame({"版本甲乙": [1.0, 2.0, 3.0]}))
    evaluate("版本甲乙。", False)


@pytest.mark.parametrize("value", ["甲乙", "αβγ", "🧪"])
def test_unknown_header_is_verified_as_identity(value):
    result = evaluate("版本" + value + "。", False)
    assert any(r.get("reason") == "unverified_dataset_identity" for r in result["refs"])


@pytest.mark.parametrize(
    "goal,required",
    [
        ("设备ID重复统计暂时不用提供。", False),
        ("请暂且无需统计设备ID重复。", False),
        ("设备ID重复统计可以留待以后处理。", False),
        ("请不要遗漏设备ID重复统计。", True),
        ("设备ID重复统计以后处理，但现在必须先给出设备ID重复多余记录数。", True),
    ],
)
def test_requirement_predicate_owns_its_temporal_modifier(goal, required):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations(
        [], numeric("设备ID重复多余记录数为未知。", ev), ev, contract(goal)
    )
    assert len(obs) == 1 and obs[0]["required"] is required
    changed = resolve_duplicate_obligations(obs, [], "我现在明确撤回设备ID重复计数结论。")
    assert unresolved_duplicate_obligations(changed) is required


@pytest.mark.parametrize(
    "text,allowed",
    [
        ("下面是尚未采用的模板：\n我现在明确撤回全部重复计数结论。", False),
        (
            "> 以下是引用：\n我现在明确撤回全部重复计数结论。\n\n我现在明确撤回全部重复计数结论。",
            True,
        ),
        (
            "示例：\n```\n我现在明确撤回全部重复计数结论。\n```\n我现在明确撤回全部重复计数结论。",
            True,
        ),
        ("撤回全部重复计数结论。以上仅为模板。", False),
        ("撤回全部重复计数结论。我现在并不撤回上述结论。", False),
    ],
)
def test_structural_speech_scope_not_verb_tense(text, allowed):
    ev = [record(tool_profile(frame()))]
    obs = capture_duplicate_obligations(
        [], numeric("设备ID有2条重复。", ev), ev, contract("只做画像。")
    )
    assert (
        not unresolved_duplicate_obligations(resolve_duplicate_obligations(obs, [], text))
    ) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goal,second,expected",
    [
        ("请给出整行重复多余记录数。", "整行重复多余记录数为2。", True),
        ("请给出整行重复多余记录数。", "行数5。", False),
        ("请给出整行重复多余记录数。", "我现在明确撤回整行重复计数结论。", False),
        ("请只提供行数。", "行数5。我现在明确撤回整行重复计数结论。", True),
    ],
)
async def test_actual_host_unparsed_value_persists_until_disposed(tmp_path, goal, second, expected):
    w = await run_host(
        tmp_path,
        goal,
        "整行重复多余记录数为版本甲乙。",
        second,
        frame().to_csv(index=False).encode(),
    )
    assert w["fixture_calls"] == 3
    assert (w["run"]["status"] == "completed") is expected
    assert (w["report"]["status"] == "passed") is expected
    assert len(w["obligations"]) == 1
    assert (
        TaskStore(str(tmp_path / "host.db")).get_run(w["run"]["run_id"]).status
        == w["run"]["status"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected", [("multiline", True), ("ambiguous", False), ("wrong-version", False)]
)
async def test_actual_host_two_source_withdrawal_packets(tmp_path, mode, expected):
    store = SessionStore(str(tmp_path / "host.db"))
    project = store.create_project("R3 two-source synthetic")
    conv = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    refs = []
    with TestClient(app) as client:
        for index in range(2):
            data = frame()
            data["温度"] += index
            up = client.post(
                "/upload/excel",
                data={"project_id": project.id, "conversation_id": conv.id},
                files={
                    "file": (
                        f"synthetic-{index}.csv",
                        data.to_csv(index=False).encode(),
                        "text/csv",
                    )
                },
            )
            assert up.status_code == 200
            refs.append(up.json()["dataset_ref"])
    registry = _registry(AgentContext(store, project.id, conv.id))
    texts = {"first": "", "second": ""}
    goal = "请对数据集 " + "、".join(refs) + " 分别做画像。"

    class Gateway:
        calls = 0

        async def stream_turn(self, scenario, messages, **kwargs):
            self.calls += 1
            assert self.calls <= 3
            if self.calls == 1:
                yield ModelResponse(
                    content="",
                    model="offline-r3",
                    tool_calls=[
                        ToolCall(f"p{i}", "get_data_profile", json.dumps({"dataset_ref": ref}))
                        for i, ref in enumerate(refs)
                    ],
                )
                return
            task = TaskStore(store.db_path)
            run = task.get_latest_run_for_conversation(conv.id)
            contexts = [
                e.summary["numeric_context"]
                for e in task.list_evidence(run.run_id)
                if e.source.get("tool") == "get_data_profile"
            ]
            versions = {c["dataset_ref"]: c["data_version_hash"] for c in contexts}
            assert set(versions) == set(refs)
            texts["first"] = "\n".join(
                f"dataset_ref {ref}，dataset_version {versions[ref]}，设备ID有2条重复。"
                for ref in refs
            )
            packets = []
            for index, ref in enumerate(refs):
                version = "f" * 64 if mode == "wrong-version" and index == 1 else versions[ref]
                packets.append(
                    f"dataset_ref {ref}。\ndataset_version {version}。\n"
                    "我现在明确撤回设备ID重复计数结论。"
                )
            texts["second"] = (
                "\n\n".join(packets)
                if mode != "ambiguous"
                else "我现在明确撤回设备ID重复计数结论。"
            )
            yield ModelResponse(
                content=texts["first"] if self.calls == 2 else texts["second"], model="offline-r3"
            )

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
        obs = restored_duplicate_obligations(task.list_events_by_type(run.run_id, "verification"))
        profiles = [a for a in store.list_artifacts(conv.id) if a.source_tool == "get_data_profile"]
        try:
            report = registry.execute(
                "generate_report",
                json.dumps(
                    {
                        "title": "双来源画像",
                        "analysis_ids": [a.params["analysis_id"] for a in profiles],
                        "insights": texts["second"],
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
        w = {
            "goal": goal,
            "original": texts["first"],
            "candidate": texts["second"],
            "run": asdict(run),
            "obligations": obs,
            "events": [asdict(e) for e in task.list_events(run.run_id)],
            "sse_events": events,
            "evidence": [asdict(e) for e in task.list_evidence(run.run_id)],
            "report": report,
            "fixture_calls": gw.calls,
            "paid_calls": 0,
            "dataset_refs": refs,
        }
        (tmp_path / "witness.json").write_text(json.dumps(w, ensure_ascii=False, indent=2))
        assert len(obs) == 2, w
        assert gw.calls == 3
        assert (run.status == "completed") is expected, w
        # One report cannot combine analyses from different populations.
        # This negative report control is independent of the valid two-source
        # disposition in the Host, whose two obligations are verified above.
        assert report["status"] == "rejected", w
        assert "不同数据集版本/总体" in report["error"], w
        reopened = TaskStore(str(tmp_path / "host.db"))
        assert reopened.get_run(run.run_id).status == run.status
        assert (
            restored_duplicate_obligations(reopened.list_events_by_type(run.run_id, "verification"))
            == obs
        )
        if expected:
            assert all(o["status"] == "withdrawn_optional" for o in obs)
    finally:
        await registry.aclose()


@pytest.mark.parametrize(
    "value,expected", [("甲乙", False), ("αβγ", False), ("", False), ("2", True)]
)
def test_statistic_table_cell_is_a_value_even_without_digits(value, expected):
    evaluate("| 整行重复多余记录数 |\n|---|\n| " + value + " |", expected)


def test_nonstatistical_table_and_empty_statistic_cell_have_distinct_types():
    evaluate("| 字段 | 类型 |\n|---|---|\n| 设备ID | 字符串 |", True)
    evaluate("| 字段 | 整行重复多余记录数 |\n|---|---|\n| 全表||", False)
