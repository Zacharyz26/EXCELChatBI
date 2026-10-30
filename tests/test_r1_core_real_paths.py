"""R1 核心门槛：真实上传/工具/SQLite，离线脚本模型，不证明真实模型主链。

期待来自手工业务契约及 CSV 独立计数，安全负例不借用被测 verifier 的判定。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import socket
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext, AgentToolError
from fastapi.testclient import TestClient
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_tools import _registry
from tests.test_manual02_profile_contract import EXPLICIT_ANSWER as EXACT_COUNTS
from tests.test_profile_claim_binding import EXPLICIT_ANSWER as DIAGNOSES

ROOT = Path(__file__).resolve().parents[1]
PROFILE_GOAL = "请给出当前数据的画像和数据质量说明，暂不筛选或清洗。"
FILTER_GOAL = "筛选频道=A且状态=已播出的记录，说明筛选前和筛选后的行数。"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """实际应用/工具均在进程内；任何联网企图使测试失败。"""

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("R1 core path cannot make network/model requests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


async def real_host(
    tmp_path: Path,
    *,
    tool: str,
    operation: dict[str, Any],
    goal: str,
    answer: str,
    follow_profile: bool = False,
) -> dict[str, Any]:
    """只脚本化模型选择；上传、画像/变换、准入、Evidence及完成门全部真实。"""
    body = (ROOT / "examples/handoff/broadcast.csv").read_bytes()
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == 75
    assert sum(row["频道"] == "A" and row["状态"] == "已播出" for row in rows) == 38
    store = SessionStore(str(tmp_path / "core.sqlite3"))
    project = store.create_project("R1 independent synthetic core")
    conversation = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        response = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conversation.id},
            files={"file": ("broadcast.csv", body, "text/csv")},
        )
    assert response.status_code == 200, response.text
    ref = response.json()["dataset_ref"]
    args = {"dataset_ref": ref, **operation}

    class LocalResponses:
        calls = 0

        async def stream_turn(self, *_a: Any, **_kw: Any):
            self.calls += 1
            if self.calls > (4 if follow_profile else 3):
                raise ModelBudgetExceeded("Offline candidate admission exhausted; no provider")
            if self.calls == 1:
                yield ModelResponse(
                    content="",
                    model="offline-core",
                    tool_calls=[
                        ToolCall(
                            id="actual-tool",
                            name=tool,
                            arguments=json.dumps(args, ensure_ascii=False),
                        )
                    ],
                )
            elif self.calls == 2 and follow_profile:
                children = [d for d in store.list_datasets(project.id) if d.parent_ref == ref]
                assert len(children) == 1
                yield ModelResponse(
                    content="",
                    model="offline-core",
                    tool_calls=[
                        ToolCall(
                            id="child-profile",
                            name="get_data_profile",
                            arguments=json.dumps({"dataset_ref": children[0].ref}),
                        )
                    ],
                )
            else:
                yield ModelResponse(content=answer, model="offline-core")

    gateway = LocalResponses()
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        events = [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=goal,
                gateway=gateway,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=8, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
    run_id = next(json.loads(e["data"])["run_id"] for e in events if e["event"] == "meta")
    # New objects force persistence reads rather than trusting the live Host's cache.
    reopened = SessionStore(str(tmp_path / "core.sqlite3"))
    tasks = TaskStore(reopened.db_path)
    run = tasks.get_run(run_id)
    assert run is not None
    invocations = tasks.list_invocations(run_id)
    records = tasks.list_evidence(run_id)
    claims = tasks.list_claims(run_id)
    artifacts = reopened.list_artifacts(conversation.id)
    witness = {
        "answer": answer,
        "goal": goal,
        "arguments": args,
        "source_ref": ref,
        "run": asdict(run),
        "contract": tasks.get_contract(run_id),
        "evidence": [asdict(r) for r in records],
        "claims": [asdict(c) for c in claims],
        "invocations": [asdict(i) for i in invocations],
        "artifacts": [asdict(a) for a in artifacts],
        "events": events,
        "offline_script_turns": gateway.calls,
        "real_model_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert any(i.tool_name == tool and i.status == "succeeded" for i in invocations), witness
    assert records and all(r.run_id == run_id for r in records)
    assert any(r.source.get("tool") == tool for r in records)
    return {
        "store": reopened,
        "tasks": tasks,
        "run": run,
        "records": records,
        "claims": claims,
        "artifacts": artifacts,
        "source_ref": ref,
        "witness": witness,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,answer,expected",
    [
        ("exact-counts", EXACT_COUNTS, True),
        ("diagnosis-with-anaphor", DIAGNOSES, True),
        (
            "unsupported-groups",
            EXACT_COUNTS.replace("整行重复多余记录数为1", "整行重复组数为1"),
            False,
        ),
        ("unsupported-eight-confirmed", EXACT_COUNTS + "\n八个异常数据点均已核实。", False),
    ],
)
async def test_profile_real_host_report_and_reopen(tmp_path, monkeypatch, case, answer, expected):
    result = await real_host(
        tmp_path, tool="get_data_profile", operation={}, goal=PROFILE_GOAL, answer=answer
    )
    run, store = result["run"], result["store"]
    assert (run.status == "completed") is expected, result["witness"]
    profile = next(a for a in result["artifacts"] if a.source_tool == "get_data_profile")
    assert profile.payload["profile"]["row_count"] == 75
    metric = next(
        c for c in profile.payload["profile"]["columns"] if c["name"] == "播出时长（分钟）"
    )
    assert metric["null_count"] == 1 and metric["max"] == 999
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    from packages.common.config import get_settings

    get_settings.cache_clear()
    registry = _registry(
        AgentContext(store, run.project_id, run.conversation_id, run_id=run.run_id)
    )
    args = {
        "analysis_ids": [profile.params["analysis_id"]],
        "title": "广播画像",
        "insights": answer,
        "include_pdf": False,
    }
    try:
        if not expected:
            with pytest.raises(AgentToolError):
                registry.execute("generate_report", json.dumps(args, ensure_ascii=False))
        else:
            report = registry.execute("generate_report", json.dumps(args, ensure_ascii=False))
            data = Path(report["md_path"]).read_bytes()
            assert answer in data.decode()
            assert report["validation"]["status"] == "passed"
            assert report["validation"]["content_sha256"] == hashlib.sha256(data).hexdigest()
            (tmp_path / "report-result.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            (tmp_path / "report.md").write_bytes(data)
    finally:
        await registry.aclose()
        get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected",
    [
        ("处理结果：筛选前75行，筛选后38行。", True),
        ("处理结果：筛选前38行，筛选后75行。", False),
    ],
)
async def test_filter_real_host_persisted_roles(tmp_path, answer, expected):
    await assert_filter_run(tmp_path, answer=answer, expected=expected, goal=FILTER_GOAL)


async def assert_filter_run(tmp_path, *, answer, expected, goal, follow_profile=False):
    result = await real_host(
        tmp_path,
        tool="transform_dataset",
        operation={
            "filters": [
                {"column": "频道", "op": "==", "value": "A"},
                {"column": "状态", "op": "==", "value": "已播出"},
            ]
        },
        goal=goal,
        answer=answer,
        follow_profile=follow_profile,
    )
    assert (result["run"].status == "completed") is expected, result["witness"]
    from packages.common.dataset_store import load_dataframe

    transform_record = next(
        e for e in result["records"] if e.source.get("tool") == "transform_dataset"
    )
    child = result["store"].get_dataset(transform_record.summary["fact_projection"]["output_ref"])
    assert child is not None and child.parent_ref == result["source_ref"]
    assert child.transform["filters"] == [
        {"column": "频道", "op": "==", "value": "A"},
        {"column": "状态", "op": "==", "value": "已播出"},
    ]
    assert len(load_dataframe(child.ref)) == 38
    assert len(load_dataframe(result["source_ref"])) == 75
    facts = transform_record.summary["fact_projection"]
    assert facts["input_ref"] == result["source_ref"] and facts["output_ref"] == child.ref
    assert facts["result_hash"] == transform_record.result_hash
    if expected:
        refs = [r for c in result["claims"] for r in c.value_refs]
        assert refs and all(r["supported"] for r in refs)

    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected",
    [
        ("处理结果：筛选前75行，筛选后38行。", True),
        ("处理结果：筛选前38行，筛选后75行。", False),
    ],
)
async def test_unambiguous_filter_real_host_report(tmp_path, answer, expected):
    """明确比较语法新增控制，不替代自然对象后缀的原始失败。"""
    result = await assert_filter_run(
        tmp_path,
        answer=answer,
        expected=expected,
        goal="筛选条件：频道=A，状态=已播出。说明筛选前和筛选后的行数。",
        follow_profile=True,
    )
    run, store = result["run"], result["store"]
    profile = next(a for a in result["artifacts"] if a.source_tool == "get_data_profile")
    registry = _registry(
        AgentContext(store, run.project_id, run.conversation_id, run_id=run.run_id)
    )
    args = {
        "title": "筛选结果",
        "analysis_ids": [profile.params["analysis_id"]],
        "insights": "当前数据共38行。" if expected else "当前数据共75行。",
        "include_pdf": False,
    }
    try:
        if expected:
            report = registry.execute("generate_report", json.dumps(args, ensure_ascii=False))
            data = Path(report["md_path"]).read_bytes()
            assert args["insights"] in data.decode() and report["validation"]["status"] == "passed"
            assert report["validation"]["content_sha256"] == hashlib.sha256(data).hexdigest()
            (tmp_path / "filter-report.md").write_bytes(data)
            (tmp_path / "filter-report-result.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
        else:
            with pytest.raises(AgentToolError):
                registry.execute("generate_report", json.dumps(args, ensure_ascii=False))
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_persisted_profile_provenance_gate_mutations(tmp_path):
    """以真正成功事务为正控；只篡改离线内存副本，不写回 SQLite。"""
    import copy
    from dataclasses import replace

    from apps.orchestrator.agent_loop import _restore_task_contract
    from apps.orchestrator.control.verifier import verify_completion

    result = await real_host(
        tmp_path, tool="get_data_profile", operation={}, goal=PROFILE_GOAL, answer=EXACT_COUNTS
    )
    run, tasks = result["run"], result["tasks"]
    assert run.status == "completed", result["witness"]
    contract = _restore_task_contract(tasks.get_verified_contract(run.run_id), run.run_id)
    cases = []
    for mutation in ("valid", "root", "dataset", "version", "filters", "projection", "run"):
        records = copy.deepcopy(result["records"])
        rec = records[0]
        context = rec.summary["numeric_context"]
        if mutation == "root":
            context["profile_population"]["root_ref"] = "f" * 32
        elif mutation == "dataset":
            context["dataset_ref"] = "e" * 32
        elif mutation == "version":
            context["data_version_hash"] = "e" * 64
        elif mutation == "filters":
            context["filters"] = [{"column": "频道", "op": "==", "value": "B"}]
        elif mutation == "projection":
            rec.summary["quality_projection"]["projection_hash"] = "0" * 64
        elif mutation == "run":
            records[0] = replace(rec, run_id="foreign-run")
        verdict = verify_completion(
            contract=contract,
            final_text=EXACT_COUNTS,
            artifacts=result["artifacts"],
            invocations=tasks.list_invocations(run.run_id),
            evidence=records,
            dataset_bindings=tasks.list_dataset_bindings(run.run_id),
        )
        cases.append(
            {
                "mutation": mutation,
                "expected_pass": mutation == "valid",
                "passed": verdict.passed,
                "verdict": asdict(verdict),
            }
        )
    (tmp_path / "provenance-mutations.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2)
    )
    assert all(c["passed"] is c["expected_pass"] for c in cases), cases
    # Persisted immutable evidence remains the original valid producer output.
    assert tasks.list_evidence(run.run_id) == result["records"]
