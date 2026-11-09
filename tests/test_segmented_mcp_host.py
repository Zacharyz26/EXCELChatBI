"""Frozen synthetic MCP Host tests: real upload/tools/SQLite, scripted model only."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from apps.api.auth import current_principal_dep
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext, AgentToolRegistry
from mcp_servers.common.client_gateway import MCPClientConfig
from mcp_servers.common.service_catalog import AGENT_MCP_SERVICE_TOOLS
from packages.common.config import get_settings
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.session.coref import ReferenceResolver
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from scripts import mcp_transport_probe as probe

from tests.test_agent_tools import _registry

ROOT = Path(__file__).resolve().parents[1]
CSV = (
    "频道,时长（分钟）\n" + "".join(f'{"A" if n <= 6 else "B"},{n}\n' for n in range(1, 13))
).encode()
GOAL = "请按频道分组汇总时长（分钟），并给出总计。"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key, leaf in [
        ("DATASET_DIR", "datasets"),
        ("UPLOAD_DIR", "uploads"),
        ("REPORT_DIR", "reports"),
        ("CHAT_DB_PATH", "host.sqlite3"),
    ]:
        monkeypatch.setenv(key, str(tmp_path / leaf))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def upload(store, project, conversation):
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    app.dependency_overrides[current_principal_dep] = lambda: probe._PRINCIPAL
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local.test"
    ) as client:
        response = await client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conversation.id},
            files={"file": ("mcp-host.csv", CSV, "text/csv")},
        )
    assert response.status_code == 200, response.text
    return response.json()["dataset_ref"]


class Scripted:
    def __init__(self, ref):
        self.ref = ref
        self.calls = 0

    async def stream_turn(self, _scenario, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ModelResponse(
                content="",
                model="offline-mcp-host",
                tool_calls=[
                    ToolCall(
                        "one-aggregate",
                        "aggregate_preview",
                        json.dumps(
                            {
                                "dataset_ref": self.ref,
                                "group_col": "频道",
                                "value_col": "时长（分钟）",
                                "agg": "sum",
                                "sort": "group",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
            )
        elif self.calls == 2:
            yield ModelResponse(content="时长（分钟）总计为78。", model="offline-mcp-host")
        else:
            raise ModelBudgetExceeded("Deterministic fixture exhausted: no real provider or retry")


def environment(store):
    # Synthetic settings only. Actual production AgentServiceRuntime is the server.
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(
        CHATBI_TEST_ISOLATION="1",
        APP_ENV="testing",
        CHAT_DB_PATH=str(store.db_path),
        DATASET_DIR=get_settings().dataset_dir,
        UPLOAD_DIR=get_settings().upload_dir,
        REPORT_DIR=get_settings().report_dir,
        MODEL_REGISTRY_PATH=str(ROOT / "config/models.example.yaml"),
        MCP_AGENT_SERVICE="data-tools",
        MCP_CONTEXT_SIGNING_KEY=probe.TOKEN,
        MCP_SERVICE_TOKEN=probe.TOKEN,
        PROCESS_ROLE="mcp_server",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    return env


@pytest.mark.asyncio
async def test_original_probe_establishes_real_upload_source(tmp_path):
    result = await probe.run_probe(tmp_path / "transport-result.json")
    assert result["equivalent"] and result["resource_equivalent"]
    assert result["host_reference"]["source_kind"] == "upload"
    assert result["host_reference"]["source_artifact_id"]
    assert result["http_process_exit_code"] == 0
    assert all(result["http_negative_checks"].values())


@pytest.mark.asyncio
async def test_unrelated_project_dataset_does_not_create_current_source(tmp_path):
    store = SessionStore(str(tmp_path / "source.sqlite3"))
    project = store.create_project(
        "isolated MCP source",
        owner_user_id=probe._PRINCIPAL.user_id,
        tenant_id=probe._PRINCIPAL.tenant_scope,
    )
    first = store.create_conversation(project.id)
    empty = store.create_conversation(project.id)
    await upload(store, project, first)
    found = ReferenceResolver(SessionStore(store.db_path)).resolve(
        "处理当前数据集",
        project_id=project.id,
        conversation_id=empty.id,
        principal=probe._PRINCIPAL,
    )
    assert found.status == "unresolved" and found.targets == ()
    assert found.reason_code == "execution_source_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["in_process", "stdio", "streamable_http"])
async def test_host_gateway_tool_evidence_sqlite_roundtrip(tmp_path, transport):
    store = SessionStore(str(tmp_path / "host.sqlite3"))
    project = store.create_project(
        "actual MCP Host",
        owner_user_id=probe._PRINCIPAL.user_id,
        tenant_id=probe._PRINCIPAL.tenant_scope,
    )
    conversation = store.create_conversation(project.id)
    ref = await upload(store, project, conversation)
    resolution = ReferenceResolver(store).resolve(
        "处理当前数据集",
        project_id=project.id,
        conversation_id=conversation.id,
        principal=probe._PRINCIPAL,
    )
    assert resolution.status == "resolved" and [t.dataset_ref for t in resolution.targets] == [ref]
    run_id = uuid.uuid4().hex
    env = environment(store)
    process = None
    config = MCPClientConfig(
        transport=transport,
        context_signing_key=probe.TOKEN,
        allow_in_process_fallback=False,
        max_reconnects=0,
    )
    if transport == "stdio":
        from dataclasses import replace

        config = replace(
            config,
            stdio_command=(sys.executable, "-m", "mcp_servers.agent_service.server"),
            stdio_cwd=str(ROOT),
            stdio_env={**env, "MCP_TRANSPORT": "stdio"},
        )
    elif transport == "streamable_http":
        from dataclasses import replace

        port = 18150
        url = f"http://127.0.0.1:{port}/mcp/"
        env.update(
            MCP_TRANSPORT="streamable-http",
            MCP_HTTP_HOST="127.0.0.1",
            MCP_HTTP_PORT=str(port),
            MCP_ALLOWED_HOSTS="127.0.0.1:*",
        )
        errlog = (tmp_path / "data-service.log").open("w")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_servers.agent_service.server",
            cwd=ROOT,
            env=env,
            stdout=errlog,
            stderr=errlog,
        )
        await probe._wait_http(url, process)
        config = replace(config, http_url=url, service_token=probe.TOKEN)
    context = AgentContext(
        store, project.id, conversation.id, subject_id=probe._PRINCIPAL.user_id, run_id=run_id
    )
    canonical = _registry(context)
    registry = AgentToolRegistry(
        [canonical._specs[name] for name in AGENT_MCP_SERVICE_TOOLS["data-tools"]],
        mcp_config=config,
        context=context,
    )
    model = Scripted(ref)
    try:
        events = [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=GOAL,
                gateway=model,
                registry=registry,
                locks=ConversationLockPool(),
                principal=probe._PRINCIPAL,
                run_id=run_id,
                config=AgentLoopConfig(max_model_rounds=3, max_tool_calls=2, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
        await canonical.aclose()
        if process is not None:
            if process.returncode is None:
                process.send_signal(signal.SIGINT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 10)
            if process.returncode is None:
                process.kill()
                await process.wait()
            errlog.close()
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(run_id)
    calls = tasks.list_invocations(run_id)
    evidence = tasks.list_evidence(run_id)
    arts = store.list_artifacts(conversation.id)
    witness = {
        "transport": transport,
        "model": "scripted, zero provider calls",
        "run": asdict(run) if run is not None else None,
        "events": events,
        "invocations": [asdict(x) for x in calls],
        "evidence": [asdict(x) for x in evidence],
        "bindings": [asdict(x) for x in tasks.list_dataset_bindings(run_id)],
        "artifacts": [asdict(x) for x in arts],
        "contract": tasks.get_verified_contract(run_id),
    }
    (tmp_path / "host-witness.json").write_text(
        json.dumps(witness, ensure_ascii=False, indent=2, default=str)
    )
    assert run is not None and run.status == "completed", witness
    assert len(calls) == len(evidence) == 1 and calls[0].status == "succeeded", witness
    assert calls[0].tool_name == "aggregate_preview" and calls[0].args["dataset_ref"] == ref
    e = evidence[0]
    assert e.invocation_id == calls[0].invocation_id and e.result_hash == calls[0].result_hash
    assert e.source["transport"] == transport and e.source["mcp_execution"] == "canonical_gateway"
    assert e.source["mcp_degraded"] is False and e.source["mcp_contract_equivalent"] is True
    table = next(a for a in arts if a.type == "table")
    assert table.dataset_ref == ref
    assert {r["group"]: r["value"] for r in table.payload["rows"]} == {"A": 21, "B": 57}
    assert len(table.payload["rows"]) == 2 and table.payload["total_value"] == 78
    assert (
        e.summary["numeric_context"]["data_version_hash"]
        and e.summary["numeric_context"]["dataset_ref"] == ref
    )
    reopened = TaskStore(SessionStore(store.db_path).db_path)
    assert reopened.list_invocations(run_id) == calls and reopened.list_evidence(run_id) == evidence
    assert reopened.get_run(run_id).status == "completed"
    if process is not None:
        assert process.returncode == 0
