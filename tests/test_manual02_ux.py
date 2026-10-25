"""Free actual Host recovery: targeted feedback, admission stop and no false PASS."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.profile_claims import duplicate_count_feedback
from fastapi.testclient import TestClient
from packages.models.gateway import ModelGateway
from packages.models.types import ModelBudgetExceeded, ModelResponse, Scenario, ToolCall
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_manual02_profile_contract import CAPTURE, EXPLICIT_ANSWER
from tests.test_model_gateway import _MSGS, _seed
from tests.test_model_gateway import _registry as model_registry
from tests.test_profile_subject_boundaries import record

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected,code,rounds",
    [
        ("budget", "blocked", "model_budget_exhausted", 4),
        ("budget-first", "blocked", "model_budget_exhausted", 4),
        ("round-limit", "failed", "model_round_limit_exhausted", 2),
        ("provider-failure", "failed", "model_unavailable", 4),
        ("no-response", "blocked", "empty_response", 3),
        ("explicit-direct", "completed", None, 4),
        ("explicit-repair", "completed", None, 4),
        ("wrong-count", "blocked", "unsupported_numeric_claim", 4),
        ("wrong-basis", "blocked", "unsupported_numeric_claim", 4),
        ("still-ambiguous", "blocked", "unsupported_numeric_claim", 4),
    ],
)
async def test_actual_host_repair(tmp_path, mode, expected, code, rounds):
    store = SessionStore(str(tmp_path / "host.db"))
    project = store.create_project("MANUAL-02 UX synthetic")
    conv = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        upload = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conv.id},
            files={
                "file": (
                    "broadcast.csv",
                    (ROOT / "examples/handoff/broadcast.csv").read_bytes(),
                    "text/csv",
                )
            },
        )
    assert upload.status_code == 200
    ref = upload.json()["dataset_ref"]

    class Gateway:
        attempts = 0
        accepted_turns = 0
        blocked_requests = 0
        inputs = []

        async def stream_turn(self, scenario, messages, **kwargs):
            self.attempts += 1
            self.inputs.append(copy.deepcopy([asdict(m) for m in messages]))
            if mode == "budget-first" or (mode == "budget" and self.attempts > 2):
                self.blocked_requests += 1
                raise ModelBudgetExceeded("synthetic pre-dispatch guard")
            if self.attempts > 2 and mode == "provider-failure":
                raise RuntimeError("synthetic provider unavailable")
            self.accepted_turns += 1
            if self.attempts == 1:
                yield ModelResponse(
                    content="",
                    model="offline-ux",
                    tool_calls=[
                        ToolCall("profile", "get_data_profile", json.dumps({"dataset_ref": ref}))
                    ],
                )
                return
            if self.attempts == 2:
                text = EXPLICIT_ANSWER if mode == "explicit-direct" else CAPTURE["answer"]
            else:
                assert self.attempts == 3, "No unbounded recovery or hidden fourth request"
                feedback = messages[-1].content
                assert messages[-1].role == "user"
                assert "整行重复多余记录数" in feedback and "完整字段名" in feedback
                assert "参与重复的全部记录数" in feedback and "组数" in feedback
                assert "不要要求用户替你选择口径" in feedback
                assert "不得自行相减" in feedback
                assert any(m.content == CAPTURE["answer"] for m in messages)
                text = {
                    "explicit-repair": EXPLICIT_ANSWER,
                    "wrong-count": EXPLICIT_ANSWER.replace(
                        "记录ID重复多余记录数为1", "记录ID重复多余记录数为99"
                    ),
                    "wrong-basis": EXPLICIT_ANSWER.replace(
                        "记录ID重复多余记录数为1", "记录ID参与重复记录数为1"
                    ),
                    "still-ambiguous": CAPTURE["answer"],
                    "no-response": "",
                }[mode]
            yield ModelResponse(content=text, model="offline-ux")

    gateway = Gateway()
    registry = _registry(AgentContext(store, project.id, conv.id))
    try:
        events = [
            e
            async for e in stream_agent_chat(
                conversation_id=conv.id,
                project_id=project.id,
                store=store,
                user_text=CAPTURE["goal"],
                gateway=gateway,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(
                    max_model_rounds=rounds, max_tool_calls=12, max_parallel_tools=1
                ),
            )
        ]
        runid = next(json.loads(e["data"])["run_id"] for e in events if e["event"] == "meta")
        terminal = [json.loads(e["data"]) for e in events if e["event"] in {"done", "error"}][-1]
        reopened = SessionStore(store.db_path)
        task = TaskStore(reopened.db_path)
        run = task.get_run(runid)
        claims = task.list_claims(runid)
        evidence = task.list_evidence(runid)
        persisted_events = task.list_events(runid)
        folder = tmp_path / "witnesses" / mode
        folder.mkdir(parents=True)
        witness = {
            "mode": mode,
            "original_answer": CAPTURE["answer"],
            "run": asdict(run),
            "terminal": terminal,
            "events": events,
            "persisted_events": [asdict(e) for e in persisted_events],
            "claims": [asdict(c) for c in claims],
            "evidence": [asdict(e) for e in evidence],
            "gateway_inputs": gateway.inputs,
            "gateway_attempts": gateway.attempts,
            "scripted_accepted_turns": gateway.accepted_turns,
            "blocked_requests": gateway.blocked_requests,
            "paid_requests": 0,
        }
        (folder / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
        with sqlite3.connect(f'file:{tmp_path/"host.db"}?mode=ro', uri=True) as src:
            with sqlite3.connect(folder / "host.db") as dst:
                src.backup(dst)
        assert run.status == expected, terminal
        assert terminal.get("code") == code, terminal
        assert not any("clarification" in e["event"] for e in events)
        if mode not in {"budget-first", "explicit-direct"}:
            repair = [
                e.payload["numeric_repair"]
                for e in persisted_events
                if "numeric_repair" in e.payload
            ]
            assert repair and all(r["kind"] == "duplicate_count_basis" for r in repair)
        if expected == "completed":
            assert all(r.get("supported") for c in claims for r in c.value_refs)
            delivered = "".join(
                json.loads(e["data"])["delta"] for e in events if e["event"] == "text.delta"
            )
            assert EXPLICIT_ANSWER in delivered
            assert "存在 1 条完全重复的行记录" not in delivered
            profile = next(
                a for a in reopened.list_artifacts(conv.id) if a.source_tool == "get_data_profile"
            )
            result = registry.execute(
                "generate_report",
                json.dumps(
                    {
                        "title": "节目画像",
                        "analysis_ids": [profile.params["analysis_id"]],
                        "insights": EXPLICIT_ANSWER,
                        "include_pdf": False,
                    }
                ),
            )
            assert result["validation"]["status"] == "passed"
            (folder / "actual-report.md").write_bytes(Path(result["md_path"]).read_bytes())
        else:
            assert not any(
                e["event"] == "done" and json.loads(e["data"]).get("run_status") == "completed"
                for e in events
            )
            if mode != "budget-first":
                assert "重复计数" in terminal["message"] and "任务未完成" in terminal["message"]
        if mode.startswith("budget"):
            assert terminal["retryable"] is False
            assert run.terminal_reason == "model_budget_exhausted"
            assert gateway.blocked_requests == 1
            assert "未向供应商发送" in terminal["message"]
            assert persisted_events[-1].payload["message"] == terminal["message"]
        if mode in {"wrong-count", "wrong-basis", "still-ambiguous"}:
            assert any(not r.get("supported") for c in claims for r in c.value_refs)
            assert not any("deterministic_repairs" in e.payload for e in persisted_events)
    finally:
        await registry.aclose()


@pytest.mark.parametrize(
    "kind", ["valid", "other-tool", "other-kind", "no-profile", "no-duplicate"]
)
def test_targeted_feedback_requires_current_profile(kind):
    evidence = record(CAPTURE["tool_result"])
    text = CAPTURE["answer"] if kind != "no-duplicate" else "播出时长（分钟）最大值998。"
    claims = extract_numeric_claims(final_text=text, goal=CAPTURE["goal"], evidence=[evidence])
    if kind == "other-tool":
        evidence = replace(evidence, source={"tool": "some_other_tool"})
    if kind == "other-kind":
        evidence = replace(evidence, kind="memory")
    if kind == "no-profile":
        evidence = replace(evidence, summary={"value_index": []})
    feedback = duplicate_count_feedback(claims, [evidence])
    assert (feedback is not None) is (kind == "valid")
    if feedback:
        assert "75" not in feedback and "74" not in feedback and "99" not in feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["complete", "stream_turn", "stream"])
async def test_budget_admission_error_never_falls_back(method):
    class Adapter:
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise ModelBudgetExceeded("pre-dispatch guard")

        async def stream_turn(self, *args, **kwargs):
            self.calls += 1
            raise ModelBudgetExceeded("pre-dispatch guard")
            yield  # pragma: no cover

        async def stream(self, *args, **kwargs):
            self.calls += 1
            raise ModelBudgetExceeded("pre-dispatch guard")
            yield  # pragma: no cover

    gateway = ModelGateway(model_registry())
    primary, backup = Adapter(), Adapter()
    _seed(gateway, {"primary": primary, "backup": backup})
    with pytest.raises(ModelBudgetExceeded):
        if method == "complete":
            await gateway.complete(Scenario.CORE_REASONING, _MSGS)
        else:
            async for _ in getattr(gateway, method)(Scenario.CORE_REASONING, _MSGS):
                pass
    assert primary.calls == 1 and backup.calls == 0
