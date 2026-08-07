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
from mcp_servers.common.client_gateway import (
    MCPResourceNotificationBuffer,
    OfficialSDKSessionTransport,
)
from mcp_servers.common.contracts import MCPProtocolError, MCPRequestContext, stable_hash
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
class ResourceTransportResult:
    result_hash: str
    initial_catalog_version: str
    published_catalog_version: str
    page_count: int
    notification_kinds: tuple[str, ...]
    cross_scope_rejected: bool
    unsubscribe_quiet: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "result_hash": self.result_hash,
            "initial_catalog_version": self.initial_catalog_version,
            "published_catalog_version": self.published_catalog_version,
            "page_count": self.page_count,
            "notification_kinds": list(self.notification_kinds),
            "cross_scope_rejected": self.cross_scope_rejected,
            "unsubscribe_quiet": self.unsubscribe_quiet,
        }


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
    resources: ResourceTransportResult

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
            "resources": self.resources.public_dict(),
        }


def _context(
    *,
    expired: bool = False,
    memory_snapshot_id: str = "0" * 32,
    project_id: str = "probe-project",
    conversation_id: str = "probe-conversation",
) -> MCPRequestContext:
    deadline = datetime.now(UTC) + (timedelta(seconds=-1) if expired else timedelta(minutes=1))
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
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
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


def _write_resource_state(
    path: Path,
    *,
    revision: int,
    resource_count: int,
) -> None:
    temporary = path.with_name(f"{path.name}.next")
    temporary.write_text(
        json.dumps(
            {"revision": revision, "resource_count": resource_count},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _publish_resource_state(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _write_resource_state(
        path,
        revision=int(raw["revision"]) + 1,
        resource_count=int(raw["resource_count"]) + 1,
    )


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
            source_hash=hashlib.sha256(confirmation.content.encode("utf-8")).hexdigest(),
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


async def _exercise_resources(
    session: ClientSession,
    notifications: MCPResourceNotificationBuffer,
    *,
    state_path: Path,
    memory_snapshot_id: str,
    project_id: str,
    conversation_id: str,
) -> ResourceTransportResult:
    transport = OfficialSDKSessionTransport(
        session,
        context_signing_key=TOKEN,
        resource_notifications=notifications,
    )
    context = _context(
        memory_snapshot_id=memory_snapshot_id,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    first = await transport.list_resource_page(context)
    if len(first.resources) != 2 or first.next_cursor is None:
        raise RuntimeError("MCP Resource 首分页未按固定页大小返回")
    resumed = replace(
        context,
        run_id="probe-resource-resumed-run",
        invocation_id="probe-resource-resumed-invocation",
        idempotency_key="probe-resource-resumed-idempotency",
    )
    second = await transport.list_resource_page(
        resumed,
        cursor=first.next_cursor,
    )
    if len(second.resources) != 1 or second.next_cursor is not None:
        raise RuntimeError("MCP Resource 跨 Run 分页恢复不一致")
    if second.catalog_version != first.catalog_version:
        raise RuntimeError("MCP Resource 分页目录版本漂移")

    resources = (*first.resources, *second.resources)
    contents = await transport.read_resource(resources[0].uri, context)
    try:
        await transport.list_resource_page(
            replace(context, project_id="cross-project"),
        )
    except MCPProtocolError as exc:
        cross_scope_rejected = exc.code == "resource_not_found"
    else:
        cross_scope_rejected = False
    if not cross_scope_rejected:
        raise RuntimeError("MCP Resource 跨项目发现未失败关闭")

    await transport.subscribe_resource(resources[0].uri, context)
    _publish_resource_state(state_path)
    changed = await transport.next_resource_notification(timeout_seconds=2)
    updated = await transport.next_resource_notification(timeout_seconds=2)
    if (
        changed.kind != "list_changed"
        or updated.kind != "updated"
        or updated.uri != resources[0].uri
        or changed.catalog_version != updated.catalog_version
        or changed.previous_catalog_version != first.catalog_version
    ):
        raise RuntimeError("MCP Resource 版本化通知不一致")

    published = await transport.list_resource_page(context)
    if published.catalog_version != changed.catalog_version:
        raise RuntimeError("MCP Resource 通知目录版本与重新发现结果不一致")
    await transport.unsubscribe_resource(resources[0].uri, context)
    _publish_resource_state(state_path)
    try:
        await transport.next_resource_notification(timeout_seconds=0.25)
    except TimeoutError:
        unsubscribe_quiet = True
    else:
        unsubscribe_quiet = False
    if not unsubscribe_quiet:
        raise RuntimeError("MCP Resource 退订后仍收到通知")

    return ResourceTransportResult(
        result_hash=stable_hash(
            {
                "resources": [item.to_protocol_dict() for item in resources],
                "contents": {
                    "uri": contents.uri,
                    "text": contents.text,
                    "mime_type": contents.mime_type,
                    "metadata": contents.metadata or {},
                },
            }
        ),
        initial_catalog_version=first.catalog_version,
        published_catalog_version=published.catalog_version,
        page_count=2,
        notification_kinds=(changed.kind, updated.kind),
        cross_scope_rejected=cross_scope_rejected,
        unsubscribe_quiet=unsubscribe_quiet,
    )


async def _exercise_session(
    session: ClientSession,
    notifications: MCPResourceNotificationBuffer,
    *,
    name: str,
    arguments: dict[str, Any],
    resource_state_path: Path,
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
    resources = await _exercise_resources(
        session,
        notifications,
        state_path=resource_state_path,
        memory_snapshot_id=memory_snapshot_id,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    return TransportResult(
        name=name,
        result_hash=stable_hash(success.structuredContent),
        protocol_version=str(initialized.protocolVersion),
        tool_count=len(listed.tools),
        latency_ms=(time.perf_counter() - started) * 1000,
        error_codes=errors,
        session_created=session_created,
        graceful_close=True,
        resources=resources,
    )


async def _probe_stdio(
    dataset_dir: Path,
    resource_state_path: Path,
    arguments: dict[str, Any],
    memory_snapshot_id: str,
    project_id: str,
    conversation_id: str,
) -> TransportResult:
    _write_resource_state(resource_state_path, revision=1, resource_count=3)
    env = {
        "DATASET_DIR": str(dataset_dir),
        "MCP_TRANSPORT": "stdio",
        "MCP_PROBE_DELAY_SECONDS": "0",
        "MCP_CONTEXT_SIGNING_KEY": TOKEN,
        "MCP_PROBE_RESOURCE_STATE": str(resource_state_path),
        "MCP_PROBE_PROJECT_ID": project_id,
        "MCP_PROBE_SUBJECT_ID": _PRINCIPAL.user_id,
        "MCP_RESOURCE_PAGE_SIZE": "2",
        "MCP_RESOURCE_POLL_INTERVAL_SECONDS": "0.05",
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
            notifications = MCPResourceNotificationBuffer()
            async with ClientSession(
                read_stream,
                write_stream,
                message_handler=notifications,
            ) as session:
                return await _exercise_session(
                    session,
                    notifications,
                    name="stdio",
                    arguments=arguments,
                    resource_state_path=resource_state_path,
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
                raise RuntimeError(f"Streamable HTTP probe server 提前退出: {tail}")
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
    resource_state_path: Path,
    arguments: dict[str, Any],
    memory_snapshot_id: str,
    project_id: str,
    conversation_id: str,
) -> tuple[TransportResult, dict[str, bool], int | None]:
    _write_resource_state(resource_state_path, revision=1, resource_count=3)
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
        "MCP_PROBE_RESOURCE_STATE": str(resource_state_path),
        "MCP_PROBE_PROJECT_ID": project_id,
        "MCP_PROBE_SUBJECT_ID": _PRINCIPAL.user_id,
        "MCP_RESOURCE_PAGE_SIZE": "2",
        "MCP_RESOURCE_POLL_INTERVAL_SECONDS": "0.05",
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
                notifications = MCPResourceNotificationBuffer()
                async with ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=notifications,
                ) as session:
                    result = await _exercise_session(
                        session,
                        notifications,
                        name="streamable_http",
                        arguments=arguments,
                        resource_state_path=resource_state_path,
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
                Path(temp) / "resource-state.json",
                arguments,
                host_reference["memory_snapshot_id"],
                host_reference["project_id"],
                host_reference["conversation_id"],
            )
            http_result, negative, exit_code = await _probe_http(
                dataset_dir,
                Path(temp) / "resource-state.json",
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
    resource_hashes = {
        stdio.resources.result_hash,
        http_result.resources.result_hash,
    }
    if len(resource_hashes) != 1:
        raise RuntimeError("MCP Resource 在 stdio 与 HTTP 间输出不等价")
    if (
        stdio.resources.initial_catalog_version != http_result.resources.initial_catalog_version
        or stdio.resources.published_catalog_version
        != http_result.resources.published_catalog_version
    ):
        raise RuntimeError("MCP Resource 在 stdio 与 HTTP 间目录版本不等价")
    report = {
        "schema": "chatbi-mcp-transport-probe-v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "sdk": {"package": "mcp", "version": version("mcp")},
        "protocol_version": MCP_PROTOCOL_VERSION,
        "tool": "aggregate_preview",
        "equivalent": True,
        "resource_equivalent": True,
        "resource_contract": "pagination-subscription-notification-v1",
        "host_reference": {
            "conversation_resolution_hash": host_reference["conversation_resolution_hash"],
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
