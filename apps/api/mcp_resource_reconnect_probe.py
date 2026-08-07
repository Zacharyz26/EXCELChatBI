"""Stage 5B-4 Compose gate for Resource transport equivalence and reconnect."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp_servers.agent_service.server import DomainDefinitionResourceProvider
from mcp_servers.common.client_gateway import (
    ManagedMCPClientGateway,
    MCPClientConfig,
    OfficialSDKClientTransport,
)
from mcp_servers.common.contracts import (
    MCPProtocolError,
    MCPRequestContext,
    MCPResourceContents,
    MCPResourceDescriptor,
    MCPToolDescriptor,
    stable_hash,
)
from packages.common.config import Settings, get_settings
from packages.governance.permissions import Principal
from packages.knowledge.domain_models import DomainDefinitionDraft
from packages.knowledge.domain_store import DomainDefinitionStore
from packages.session.store import SessionStore

_PRINCIPAL = Principal(user_id="local-user", tenant_id="local")


def _context(
    *,
    project_id: str,
    conversation_id: str,
    run_id: str = "compose-resource-run-1",
) -> MCPRequestContext:
    return MCPRequestContext(
        subject_id=_PRINCIPAL.user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        run_id=run_id,
        plan_version=0,
        step_id="compose-resource-step",
        invocation_id=f"compose-resource-invocation:{run_id}",
        idempotency_key=f"compose-resource-idempotency:{run_id}",
        permission_snapshot_id="compose-resource-permissions",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
        trace_id="compose-resource-trace",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )


def _publish_definition(
    definitions: DomainDefinitionStore,
    *,
    project_id: str,
    index: int,
) -> None:
    definitions.create_definition(
        project_id=project_id,
        principal=_PRINCIPAL,
        draft=DomainDefinitionDraft(
            semantic_key=f"metric.compose_resource_probe_{index}",
            version=1,
            title=f"Compose Resource probe {index}",
            description="Stage 5B-4 transport and reconnect fixture.",
            formula={
                "tool": "aggregate_preview",
                "arguments": {
                    "group_concept": f"dimension.probe_{index}",
                    "value_concept": f"measure.probe_{index}",
                    "agg": "sum",
                    "sort": "group",
                    "limit": 100,
                },
            },
            grain=(f"dimension.probe_{index}",),
            scope={"dataset_type": "tabular"},
            owner="stage-5b-compose-gate",
            source_ref=f"urn:chatbi:stage-5b-4:definition:{index}",
            effective_from="2026-01-01T00:00:00Z",
        ),
        idempotency_key=f"stage-5b-4:{project_id}:{index}",
    )


def _seed(settings: Settings) -> tuple[str, str, DomainDefinitionStore]:
    sessions = SessionStore(settings.chat_db_path)
    project = sessions.create_project(
        f"Stage 5B-4 Compose probe {uuid.uuid4().hex[:8]}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = sessions.create_conversation(project.id, "Resource reconnect")
    definitions = DomainDefinitionStore(sessions)
    for index in range(1, 4):
        _publish_definition(definitions, project_id=project.id, index=index)
    return project.id, conversation.id, definitions


async def _expected_tools(config: MCPClientConfig) -> tuple[MCPToolDescriptor, ...]:
    transport = OfficialSDKClientTransport(config)
    try:
        return await transport.list_tools()
    finally:
        await transport.aclose()


def _gateway(
    config: MCPClientConfig,
    expected: tuple[MCPToolDescriptor, ...],
) -> ManagedMCPClientGateway:
    names = frozenset(item.name for item in expected)
    return ManagedMCPClientGateway(
        config=config,
        expected=expected,
        allowed_tools=names,
        transport_factory=lambda: OfficialSDKClientTransport(config),
    )


async def _resource_projection(
    gateway: ManagedMCPClientGateway,
    context: MCPRequestContext,
) -> tuple[dict[str, Any], str]:
    first = await gateway.list_resource_page(context)
    if len(first.resources) != 2 or first.next_cursor is None:
        raise RuntimeError("Compose Resource 首分页未按 E2E 固定页大小返回")
    second = await gateway.list_resource_page(
        replace(
            context,
            run_id="compose-resource-resumed-run",
            invocation_id="compose-resource-resumed-invocation",
            idempotency_key="compose-resource-resumed-idempotency",
        ),
        cursor=first.next_cursor,
    )
    if len(second.resources) != 2 or second.next_cursor is not None:
        raise RuntimeError("Compose Resource 跨 Run 分页恢复不一致")
    if second.catalog_version != first.catalog_version:
        raise RuntimeError("Compose Resource 分页目录版本漂移")
    resources = (*first.resources, *second.resources)
    catalog_uri = DomainDefinitionResourceProvider.catalog_uri(context.project_id)
    if resources[0].uri != catalog_uri:
        raise RuntimeError("Compose Resource 项目目录未稳定排在首位")
    contents = await gateway.read_resource(catalog_uri, context)
    return (
        {
            "resources": [_descriptor_dict(item) for item in resources],
            "contents": _contents_dict(contents),
            "page_count": 2,
        },
        catalog_uri,
    )


async def _wait_for_reconnect(
    gateway: ManagedMCPClientGateway,
    context: MCPRequestContext,
    *,
    timeout_seconds: float = 120,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            await gateway.list_resources(context)
        except MCPProtocolError as exc:
            if exc.code not in {
                "mcp_transport_disconnected",
                "mcp_authentication_failed",
            }:
                raise
        if gateway.health.generation >= 2:
            return
        await asyncio.sleep(0.25)
    raise TimeoutError("Compose knowledge-tools 重启后未恢复 Resource 会话")


async def run_probe() -> dict[str, Any]:
    settings = get_settings()
    project_id, conversation_id, definitions = _seed(settings)
    context = _context(project_id=project_id, conversation_id=conversation_id)
    raw_tokens = json.loads(settings.agent_mcp_service_tokens_json)
    service_token = raw_tokens.get("knowledge-tools") if isinstance(raw_tokens, dict) else None
    if not service_token or not settings.agent_mcp_context_signing_key:
        raise RuntimeError("Compose Resource 探针缺少 knowledge-tools 内部凭据")
    http_config = MCPClientConfig(
        transport="streamable_http",
        http_url=os.getenv(
            "MCP_RESOURCE_PROBE_HTTP_URL",
            "http://knowledge-tools:8000/mcp/",
        ),
        service_token=service_token,
        context_signing_key=settings.agent_mcp_context_signing_key,
        connect_timeout_seconds=10,
        max_reconnects=3,
    )
    stdio_config = MCPClientConfig(
        transport="stdio",
        stdio_command=(
            sys.executable,
            "-m",
            "mcp_servers.agent_service.server",
        ),
        stdio_cwd=str(Path.cwd()),
        stdio_env={
            **os.environ,
            "PROCESS_ROLE": "mcp_server",
            "MCP_AGENT_SERVICE": "knowledge-tools",
            "MCP_TRANSPORT": "stdio",
            "MCP_CONTEXT_SIGNING_KEY": settings.agent_mcp_context_signing_key,
            "MCP_RESOURCE_PAGE_SIZE": "2",
            "MCP_RESOURCE_POLL_INTERVAL_SECONDS": "0.25",
        },
        context_signing_key=settings.agent_mcp_context_signing_key,
        connect_timeout_seconds=20,
        max_reconnects=1,
    )

    expected = await _expected_tools(http_config)
    http_gateway = _gateway(http_config, expected)
    stdio_gateway = _gateway(stdio_config, expected)
    try:
        http_projection, catalog_uri = await _resource_projection(
            http_gateway,
            context,
        )
        stdio_projection, stdio_catalog_uri = await _resource_projection(
            stdio_gateway,
            context,
        )
        if stdio_catalog_uri != catalog_uri:
            raise RuntimeError("Compose stdio/HTTP 目录 URI 不一致")
        http_hash = stable_hash(http_projection)
        stdio_hash = stable_hash(stdio_projection)
        if http_hash != stdio_hash:
            raise RuntimeError("Compose stdio/HTTP Resource 结果不等价")

        await http_gateway.subscribe_resource(catalog_uri, context)
        print(
            json.dumps(
                {
                    "status": "subscribed",
                    "project_id": project_id,
                    "catalog_uri": catalog_uri,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        await _wait_for_reconnect(http_gateway, context)
        _publish_definition(definitions, project_id=project_id, index=4)
        changed = await http_gateway.next_resource_notification(timeout_seconds=10)
        updated = await http_gateway.next_resource_notification(timeout_seconds=10)
        if (
            changed.kind != "list_changed"
            or updated.kind != "updated"
            or updated.uri != catalog_uri
            or updated.catalog_version != changed.catalog_version
        ):
            raise RuntimeError("Compose 重连后 Resource 通知不一致")
        return {
            "schema": "chatbi-compose-mcp-resource-probe-v1",
            "status": "passed",
            "project_id": project_id,
            "resource_result_hash": http_hash,
            "stdio_http_equivalent": True,
            "page_count": http_projection["page_count"],
            "reconnected": True,
            "connection_generation": http_gateway.health.generation,
            "resubscribed": True,
            "notification_kinds": [changed.kind, updated.kind],
            "catalog_version": changed.catalog_version,
        }
    finally:
        await stdio_gateway.aclose()
        await http_gateway.aclose()


def _descriptor_dict(item: MCPResourceDescriptor) -> dict[str, Any]:
    return item.to_protocol_dict()


def _contents_dict(item: MCPResourceContents) -> dict[str, Any]:
    return {
        "uri": item.uri,
        "text": item.text,
        "mime_type": item.mime_type,
        "metadata": item.metadata or {},
    }


def main() -> int:
    report = asyncio.run(run_probe())
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
