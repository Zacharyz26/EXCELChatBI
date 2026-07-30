"""运行 v2.4/v2.5 stdio/Streamable HTTP 与固定 Host 引用等价探针。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb
import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_servers.common.contracts import MCPRequestContext, stable_hash
from mcp_servers.common.sdk_adapter import MCP_PROTOCOL_VERSION
from mcp_servers.dataset_ops.tools import aggregate_preview
from packages.common.config import get_settings
from packages.governance.permissions import Principal
from packages.session.coref import ReferenceResolver
from packages.session.memory_models import MemoryDraft
from packages.session.memory_refs import (
    MemoryReferenceResolver,
    memory_reference_semantic_key,
    memory_reference_summary,
)
from packages.session.memory_store import MemoryStore
from packages.session.store import SessionStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "stage0-local-probe-token"
_PRINCIPAL = Principal(user_id="probe-user", tenant_id="probe-tenant")


@dataclass(frozen=True, slots=True)
class TransportResult:
    name: str
    result_hash: str
    protocol_version: str
    tool_count: int
    latency_ms: float
    error_codes: dict[str, str]
    session_created: bool
    graceful_close: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "result_hash": self.result_hash,
            "protocol_version": self.protocol_version,
            "tool_count": self.tool_count,
            "latency_ms": round(self.latency_ms, 3),
            "error_codes": self.error_codes,
            "session_created": self.session_created,
            "graceful_close": self.graceful_close,
        }


def _context(
    *,
    expired: bool = False,
    memory_snapshot_id: str = "0" * 32,
    project_id: str = "probe-project",
    conversation_id: str = "probe-conversation",
) -> MCPRequestContext:
    deadline = datetime.now(UTC) + (
        timedelta(seconds=-1) if expired else timedelta(minutes=1)
    )
    return MCPRequestContext(
        subject_id="probe-user",
        project_id=project_id,
        conversation_id=conversation_id,
        run_id="probe-run",
        plan_version=0,
        step_id="probe-step",
        invocation_id="probe-invocation",
        idempotency_key="probe-idempotency",
        permission_snapshot_id="probe-permissions",
        memory_snapshot_id=memory_snapshot_id,
        evidence_ledger_version=0,
        trace_id="probe-trace",
        deadline_at=deadline.isoformat(),
    )


def _write_dataset(
    directory: Path,
    dataset_ref: str,
) -> dict[str, Any]:
    path = directory / f"{dataset_ref}.parquet"
    connection = duckdb.connect()
    try:
        connection.execute(
            """
            COPY (
                SELECT * FROM (
                    VALUES ('east', 10), ('east', 15), ('west', 8)
                ) AS probe(region, amount)
            ) TO ? (FORMAT PARQUET)
            """,
            [str(path)],
        )
    finally:
        connection.close()
    arguments = {
        "dataset_ref": dataset_ref,
        "group_col": "region",
        "value_col": "amount",
        "agg": "sum",
        "sort": "group",
    }
    return arguments


def _resolve_probe_reference(database: Path, dataset_ref: str) -> dict[str, str]:
    """从 Host 真相源解析同一 Dataset 和记忆映射，供双传输调用共享。"""
    session = SessionStore(str(database))
    project = session.create_project(
        "MCP reference probe",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    session.register_dataset(
        ref=dataset_ref,
        project_id=project.id,
        filename="协议样本.parquet",
        profile={"column_count": 2},
    )
    confirmation = session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="确认“协议样本”指向当前探针数据集。",
    )
    memories = MemoryStore(session, audit_recorder=lambda _event: None)
    memory = memories.remember(
        project_id=project.id,
        principal=_PRINCIPAL,
        draft=MemoryDraft(
            scope="project",
            kind="entity_mapping",
            semantic_key=memory_reference_semantic_key(
                kind="entity_mapping",
                alias="协议样本",
            ),
            content_summary=memory_reference_summary(
                kind="entity_mapping",
                alias="协议样本",
            ),
            source_type="user_confirmation",
            source_ref=confirmation.id,
            source_hash=hashlib.sha256(
                confirmation.content.encode("utf-8")
            ).hexdigest(),
            confidence=1.0,
        ),
        idempotency_key="mcp-reference-probe",
    ).record
    memories.add_link(
        memory.memory_id,
        project_id=project.id,
        principal=_PRINCIPAL,
        target_type="dataset",
        target_ref=dataset_ref,
    )
    snapshot, _ = memories.create_snapshot(
        project_id=project.id,
        conversation_id=conversation.id,
        principal=_PRINCIPAL,
    )
    conversation_reference = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        "处理当前数据集",
        project_id=project.id,
        conversation_id=conversation.id,
        principal=_PRINCIPAL,
    )
    memory_reference = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    ).resolve(
        "处理协议样本",
        project_id=project.id,
        conversation_id=conversation.id,
        memory_snapshot_id=snapshot.memory_snapshot_id,
        principal=_PRINCIPAL,
    )
    conversation_targets = [target.dataset_ref for target in conversation_reference.targets]
    memory_targets = [target.dataset_ref for target in memory_reference.targets]
    if (
        conversation_reference.status != "resolved"
        or memory_reference.status != "resolved"
        or conversation_targets != [dataset_ref]
        or memory_targets != [dataset_ref]
    ):
        raise RuntimeError("MCP 双传输探针未建立唯一 Host Dataset 绑定")
    return {
        "dataset_ref": dataset_ref,
        "project_id": project.id,
        "conversation_id": conversation.id,
        "memory_snapshot_id": snapshot.memory_snapshot_id,
        "conversation_resolution_hash": conversation_reference.resolution_hash,
        "memory_resolution_hash": memory_reference.resolution_hash,
        "target_ref_hash": stable_hash(dataset_ref),
    }


def _error_code(result: Any) -> str:
    meta = result.meta or {}
    code = meta.get("com.chatbi/error-code")
    return str(code) if code else "missing"


async def _exercise_session(
    session: ClientSession,
    *,
    name: str,
    arguments: dict[str, Any],
    session_created: bool,
    memory_snapshot_id: str,
    project_id: str,
    conversation_id: str,
) -> TransportResult:
    started = time.perf_counter()
    initialized = await session.initialize()
    listed = await session.list_tools()
    names = [tool.name for tool in listed.tools]
    if names != ["aggregate_preview"]:
        raise RuntimeError(f"{name} 工具发现不一致: {names}")
    success = await session.call_tool(
        "aggregate_preview",
        arguments,
        meta=_context(
            memory_snapshot_id=memory_snapshot_id,
            project_id=project_id,
            conversation_id=conversation_id,
        ).to_request_meta(signing_key=TOKEN),
    )
    if success.isError or not isinstance(success.structuredContent, dict):
        raise RuntimeError(f"{name} 合法调用失败")
    invalid = await session.call_tool(
        "aggregate_preview",
        {**arguments, "agg": "median"},
        meta=_context(
            memory_snapshot_id=memory_snapshot_id,
            project_id=project_id,
            conversation_id=conversation_id,
        ).to_request_meta(signing_key=TOKEN),
    )
    unknown = await session.call_tool(
        "missing_tool",
        {},
        meta=_context(
            memory_snapshot_id=memory_snapshot_id,
            project_id=project_id,
            conversation_id=conversation_id,
        ).to_request_meta(signing_key=TOKEN),
    )
    business = await session.call_tool(
        "aggregate_preview",
        {
            "dataset_ref": arguments["dataset_ref"],
            "group_col": "region",
            "agg": "sum",
        },
        meta=_context(
            memory_snapshot_id=memory_snapshot_id,
            project_id=project_id,
            conversation_id=conversation_id,
        ).to_request_meta(signing_key=TOKEN),
    )
    expired = await session.call_tool(
        "aggregate_preview",
        arguments,
        meta=_context(
            expired=True,
            memory_snapshot_id=memory_snapshot_id,
            project_id=project_id,
            conversation_id=conversation_id,
        ).to_request_meta(signing_key=TOKEN),
    )
    errors = {
        "schema": _error_code(invalid),
        "unknown_tool": _error_code(unknown),
        "business": _error_code(business),
        "deadline": _error_code(expired),
    }
    expected = {
        "schema": "invalid_arguments",
        "unknown_tool": "tool_not_found",
        "business": "tool_business_error",
        "deadline": "deadline_exceeded",
    }
    if errors != expected:
        raise RuntimeError(f"{name} 异常映射不一致: {errors}")
    return TransportResult(
        name=name,
        result_hash=stable_hash(success.structuredContent),
        protocol_version=str(initialized.protocolVersion),
        tool_count=len(listed.tools),
        latency_ms=(time.perf_counter() - started) * 1000,
        error_codes=errors,
        session_created=session_created,
        graceful_close=True,
    )


async def _probe_stdio(
    dataset_dir: Path,
    arguments: dict[str, Any],
    memory_snapshot_id: str,
    project_id: str,
    conversation_id: str,
) -> TransportResult:
    env = {
        "DATASET_DIR": str(dataset_dir),
        "MCP_TRANSPORT": "stdio",
        "MCP_PROBE_DELAY_SECONDS": "0",
        "MCP_CONTEXT_SIGNING_KEY": TOKEN,
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "scripts.mcp_transport_probe_server"],
        env=env,
        cwd=ROOT,
    )
    with tempfile.TemporaryFile(mode="w+") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                return await _exercise_session(
                    session,
                    name="stdio",
                    arguments=arguments,
                    session_created=False,
                    memory_snapshot_id=memory_snapshot_id,
                    project_id=project_id,
                    conversation_id=conversation_id,
                )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_http(url: str, process: asyncio.subprocess.Process) -> None:
    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
        for _ in range(400):
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b""
                tail = stderr.decode(errors="replace")[-1000:]
                raise RuntimeError(
                    f"Streamable HTTP probe server 提前退出: {tail}"
                )
            try:
                response = await client.get(url)
                if response.status_code == 401:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError("Streamable HTTP probe server 未就绪")


def _initialize_payload(protocol: str = MCP_PROTOCOL_VERSION) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol,
            "capabilities": {},
            "clientInfo": {"name": "chatbi-probe", "version": "1"},
        },
    }


async def _negative_http_checks(
    client: httpx.AsyncClient,
    url: str,
    arguments: dict[str, Any],
) -> dict[str, bool]:
    auth = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
    }
    no_auth = await client.post(url, json=_initialize_payload())
    bad_origin = await client.post(
        url,
        json=_initialize_payload(),
        headers={**auth, "Origin": "https://untrusted.example"},
    )
    bad_protocol = await client.post(
        url,
        json=_initialize_payload("2099-01-01"),
        headers=auth,
    )
    stale_session = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        headers={
            **auth,
            "mcp-session-id": "expired-probe-session",
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        },
    )
    cancellation = await _cancel_http_call(client, url, auth, arguments)
    checks = {
        "no_auth_rejected": no_auth.status_code == 401,
        "origin_rejected": bad_origin.status_code == 403,
        "protocol_rejected": bad_protocol.status_code == 400,
        "stale_session_rejected": stale_session.status_code == 404,
        "cancellation_acknowledged": cancellation,
    }
    if not all(checks.values()):
        raise RuntimeError(f"Streamable HTTP 负向探针失败: {checks}")
    return checks


async def _cancel_http_call(
    client: httpx.AsyncClient,
    url: str,
    auth: dict[str, str],
    arguments: dict[str, Any],
) -> bool:
    initialized = await client.post(url, json=_initialize_payload(), headers=auth)
    session_id = initialized.headers.get("mcp-session-id")
    if initialized.status_code != 200 or not session_id:
        return False
    headers = {
        **auth,
        "mcp-session-id": session_id,
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
    }
    ready = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        headers=headers,
    )
    if ready.status_code != 202:
        return False
    call = asyncio.create_task(
        client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "aggregate_preview",
                    "arguments": arguments,
                    "_meta": _context().to_request_meta(signing_key=TOKEN),
                },
            },
            headers=headers,
        )
    )
    await asyncio.sleep(0.1)
    cancelled = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 9, "reason": "stage0 probe"},
        },
        headers=headers,
    )
    response = await asyncio.wait_for(call, timeout=2)
    await client.delete(url, headers=headers)
    body = response.json()
    return (
        cancelled.status_code == 202
        and response.status_code == 200
        and body.get("error", {}).get("message") == "Request cancelled"
    )


async def _probe_http(
    dataset_dir: Path,
    arguments: dict[str, Any],
    memory_snapshot_id: str,
    project_id: str,
    conversation_id: str,
) -> tuple[TransportResult, dict[str, bool], int | None]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp/"
    env = {
        **os.environ,
        "DATASET_DIR": str(dataset_dir),
        "MCP_TRANSPORT": "streamable-http",
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_PORT": str(port),
        "MCP_SERVICE_TOKEN": TOKEN,
        "MCP_CONTEXT_SIGNING_KEY": TOKEN,
        "MCP_ALLOWED_HOSTS": "127.0.0.1:*",
        "MCP_ALLOWED_ORIGINS": "https://trusted.example",
        "MCP_PROBE_DELAY_SECONDS": "0.5",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.mcp_transport_probe_server",
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_http(url, process)
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            negative = await _negative_http_checks(client, url, arguments)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=3,
            trust_env=False,
        ) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                read_stream, write_stream, get_session_id = streams
                async with ClientSession(read_stream, write_stream) as session:
                    result = await _exercise_session(
                        session,
                        name="streamable_http",
                        arguments=arguments,
                        session_created=get_session_id() is not None,
                        memory_snapshot_id=memory_snapshot_id,
                        project_id=project_id,
                        conversation_id=conversation_id,
                    )
                    session_created = get_session_id() is not None
                    if not session_created:
                        raise RuntimeError("Streamable HTTP 未建立 stateful session")
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:
            process.kill()
            await process.wait()
    result = replace(
        result,
        session_created=session_created,
        graceful_close=process.returncode == 0,
    )
    return result, negative, process.returncode


async def run_probe(output: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chatbi-mcp-probe-") as temp:
        dataset_dir = Path(temp) / "datasets"
        dataset_dir.mkdir()
        previous_dataset_dir = os.environ.get("DATASET_DIR")
        os.environ["DATASET_DIR"] = str(dataset_dir)
        get_settings.cache_clear()
        try:
            dataset_ref = "2" * 32
            host_reference = _resolve_probe_reference(
                Path(temp) / "reference.db",
                dataset_ref,
            )
            arguments = _write_dataset(dataset_dir, host_reference["dataset_ref"])
            direct = aggregate_preview(arguments)
            direct_hash = stable_hash(direct)
            stdio = await _probe_stdio(
                dataset_dir,
                arguments,
                host_reference["memory_snapshot_id"],
                host_reference["project_id"],
                host_reference["conversation_id"],
            )
            http_result, negative, exit_code = await _probe_http(
                dataset_dir,
                arguments,
                host_reference["memory_snapshot_id"],
                host_reference["project_id"],
                host_reference["conversation_id"],
            )
        finally:
            if previous_dataset_dir is None:
                os.environ.pop("DATASET_DIR", None)
            else:
                os.environ["DATASET_DIR"] = previous_dataset_dir
            get_settings.cache_clear()
    hashes = {direct_hash, stdio.result_hash, http_result.result_hash}
    if len(hashes) != 1:
        raise RuntimeError("aggregate_preview 在直接调用、stdio、HTTP 间输出不等价")
    if {
        stdio.protocol_version,
        http_result.protocol_version,
    } != {MCP_PROTOCOL_VERSION}:
        raise RuntimeError("MCP 协议协商版本不符合固定版本")
    report = {
        "schema": "chatbi-mcp-transport-probe-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "sdk": {"package": "mcp", "version": version("mcp")},
        "protocol_version": MCP_PROTOCOL_VERSION,
        "tool": "aggregate_preview",
        "equivalent": True,
        "host_reference": {
            "conversation_resolution_hash": host_reference[
                "conversation_resolution_hash"
            ],
            "memory_resolution_hash": host_reference["memory_resolution_hash"],
            "target_ref_hash": host_reference["target_ref_hash"],
            "memory_snapshot_id": host_reference["memory_snapshot_id"],
            "same_binding_across_transports": True,
        },
        "direct_result_hash": direct_hash,
        "transports": [stdio.public_dict(), http_result.public_dict()],
        "http_negative_checks": negative,
        "http_process_exit_code": exit_code,
        "raw_data_in_report": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".data/evaluations/v2.4/mcp-transport-probe.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(run_probe(args.output.resolve()))
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "equivalent": report["equivalent"],
                "protocol_version": report["protocol_version"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
