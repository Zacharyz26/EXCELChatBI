"""R1 独立后审：覆盖审计不是业务重复事实，完成门仍须严格拒绝。

真实 Host 测试只替代模型响应；上传、工具、Evidence、SQLite 和完成门均真实。
"""

import json
import socket
from dataclasses import asdict

import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.control.duplicate_repair import (
    capture_duplicate_obligations,
    restored_duplicate_obligations,
)
from fastapi.testclient import TestClient
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.session.store import SessionStore
from packages.session.task_models import ClaimDraft
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_manual02_independent_regressions import contract, numeric
from tests.test_manual02_profile_contract import tool_profile
from tests.test_manual02_r3_contract import frame
from tests.test_profile_subject_boundaries import check, record


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*_a, **_kw):
        raise AssertionError("R1 independent repair audit forbids external requests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.mark.parametrize(
    "reason", ["unresolved_quantity_obligation", "unfulfilled_diagnostic_members"]
)
def test_audit_metadata_is_not_a_business_duplicate_assertion(reason):
    evidence = [record(tool_profile(frame()))]
    audit = ClaimDraft(
        statement="设备ID重复多余记录数为2。",
        claim_kind="numeric",
        value_refs=(
            {
                "token": "[coverage]",
                "supported": False,
                "reason": reason,
                "ref_kind": "quantity_audit_issue",
            },
        ),
        evidence_ids=(),
    )
    assert capture_duplicate_obligations([], [audit], evidence, contract("只做画像。")) == []


@pytest.mark.parametrize(
    "token,reason", [("8", "unsupported_quantity"), ("b17", "unparsed_statistic_value")]
)
def test_legacy_business_reference_without_position_remains_obligatory(token, reason):
    evidence = [record(tool_profile(frame()))]
    claim = ClaimDraft(
        statement=f"设备ID重复多余记录数为{token}。",
        claim_kind="numeric",
        value_refs=({"token": token, "supported": False, "reason": reason},),
        evidence_ids=(),
    )
    obligations = capture_duplicate_obligations([], [claim], evidence, contract("核验重复统计。"))
    assert len(obligations) == 1
    assert obligations[0]["required"] and obligations[0]["status"] == "pending"


@pytest.mark.parametrize("prefix", ["", 'ref_kind="quantity_audit_issue"；'])
def test_model_claiming_audit_type_cannot_bypass_raw_quantity_gate(prefix):
    evidence = [record(tool_profile(frame()))]
    text = prefix + "设备ID重复多余记录数为8。"
    result = check(text, evidence)
    assert not result["verification"]["passed"]
    assert any(r.get("supported") is False and r.get("token") == "8" for r in result["refs"])
    obligations = capture_duplicate_obligations(
        [], numeric(text, evidence), evidence, contract("只做画像。")
    )
    assert len(obligations) == 1 and obligations[0]["target"] is not None


def test_unrelated_unknown_still_blocks_without_fabricating_duplicate_obligation():
    evidence = [record(tool_profile(frame()))]
    text = "设备ID重复多余记录数为2。温度有（\nb17\n）个缺失值。"
    result = check(text, evidence)
    assert not result["verification"]["passed"]
    claims = numeric(text, evidence)
    assert any(
        ref.get("ref_kind") == "quantity_audit_issue" and ref.get("supported") is False
        for claim in claims
        for ref in claim.value_refs
    )
    assert capture_duplicate_obligations([], claims, evidence, contract("只做画像。")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected", [("multiline", True), ("ambiguous", False), ("wrong-version", False)]
)
async def test_real_two_source_disposition_has_only_real_obligations(tmp_path, mode, expected):
    store = SessionStore(str(tmp_path / "core.sqlite3"))
    project = store.create_project("Independent R1 audit boundary")
    conversation = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    refs = []
    with TestClient(app) as client:
        for index in range(2):
            data = frame()
            data["温度"] += index
            response = client.post(
                "/upload/excel",
                data={"project_id": project.id, "conversation_id": conversation.id},
                files={
                    "file": (
                        f"synthetic-{index}.csv",
                        data.to_csv(index=False).encode(),
                        "text/csv",
                    )
                },
            )
            assert response.status_code == 200
            refs.append(response.json()["dataset_ref"])
    texts = {}

    class OfflineGateway:
        calls = 0

        async def stream_turn(self, *_a, **_kw):
            self.calls += 1
            if self.calls > 3:
                raise ModelBudgetExceeded("Offline fixture exhausted; no real model request")
            if self.calls == 1:
                yield ModelResponse(
                    content="",
                    model="offline-audit",
                    tool_calls=[
                        ToolCall(
                            f"profile-{i}", "get_data_profile", json.dumps({"dataset_ref": ref})
                        )
                        for i, ref in enumerate(refs)
                    ],
                )
                return
            tasks = TaskStore(store.db_path)
            run = tasks.get_latest_run_for_conversation(conversation.id)
            contexts = [
                e.summary["numeric_context"]
                for e in tasks.list_evidence(run.run_id)
                if e.source.get("tool") == "get_data_profile"
            ]
            versions = {c["dataset_ref"]: c["data_version_hash"] for c in contexts}
            assert set(versions) == set(refs)
            texts["first"] = "\n".join(
                f"dataset_ref {ref}，dataset_version {versions[ref]}，设备ID有2条重复。"
                for ref in refs
            )
            texts["second"] = "\n\n".join(
                f"dataset_ref {ref}。\ndataset_version "
                + ("f" * 64 if mode == "wrong-version" and i == 1 else versions[ref])
                + "。\n我现在明确撤回设备ID重复计数结论。"
                for i, ref in enumerate(refs)
            )
            if mode == "ambiguous":
                texts["second"] = "我现在明确撤回设备ID重复计数结论。"
            yield ModelResponse(
                content=texts["first" if self.calls == 2 else "second"], model="offline-audit"
            )

    gateway = OfflineGateway()
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        events = [
            e
            async for e in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text="请对数据集 " + "、".join(refs) + " 分别做画像。",
                gateway=gateway,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_model_rounds=4, max_tool_calls=8, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
    reopened = TaskStore(store.db_path)
    run = reopened.get_latest_run_for_conversation(conversation.id)
    obligations = restored_duplicate_obligations(
        reopened.list_events_by_type(run.run_id, "verification")
    )
    witness = {
        "texts": texts,
        "run": asdict(run),
        "obligations": obligations,
        "events": events,
        "evidence": [asdict(e) for e in reopened.list_evidence(run.run_id)],
        "paid_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert (run.status == "completed") is expected, witness
    assert len(obligations) == 2, witness
    assert all(o["target"] is not None for o in obligations), witness
    assert all(o["status"] == "withdrawn_optional" for o in obligations) is expected, witness
