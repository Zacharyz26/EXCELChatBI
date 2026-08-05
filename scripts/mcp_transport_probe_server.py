"""Probe-only MCP server exposing the canonical aggregate_preview binding."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mcp_servers.common.adapter import MCPServerAdapter, MCPToolBinding
from mcp_servers.common.contracts import (
    MCPRequestContext,
    MCPResourceContents,
    MCPResourceDescriptor,
)
from mcp_servers.common.sdk_adapter import run_adapter
from mcp_servers.dataset_ops.server import build_server
from mcp_servers.dataset_ops.tools import aggregate_preview


class ProbeResourceProvider:
    """Process-safe mutable Resource fixture shared by both probe transports."""

    def __init__(self, state_path: Path, *, project_id: str, subject_id: str) -> None:
        self._state_path = state_path
        self._project_id = project_id
        self._subject_id = subject_id

    def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]:
        self._authorize(context)
        revision, resource_count = self._state()
        return tuple(
            MCPResourceDescriptor(
                uri=f"chatbi://probe-resources/resource-{index}",
                name=f"probe-resource-{index}",
                title=f"Probe resource {index}",
                description="Mutable dual-transport Resource fixture",
                metadata={
                    "com.chatbi/revision": revision,
                    "com.chatbi/index": index,
                },
            )
            for index in range(1, resource_count + 1)
        )

    def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents:
        self._authorize(context)
        revision, resource_count = self._state()
        valid_uris = {
            f"chatbi://probe-resources/resource-{index}"
            for index in range(1, resource_count + 1)
        }
        if uri not in valid_uris:
            raise FileNotFoundError(uri)
        return MCPResourceContents(
            uri=uri,
            text=json.dumps(
                {"revision": revision, "uri": uri},
                sort_keys=True,
                separators=(",", ":"),
            ),
            metadata={"com.chatbi/revision": revision},
        )

    def _authorize(self, context: MCPRequestContext) -> None:
        if (
            context.project_id != self._project_id
            or context.subject_id != self._subject_id
        ):
            raise PermissionError("probe Resource scope is not visible")

    def _state(self) -> tuple[int, int]:
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        revision = raw.get("revision")
        resource_count = raw.get("resource_count")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or isinstance(resource_count, bool)
            or not isinstance(resource_count, int)
            or resource_count < 1
        ):
            raise ValueError("MCP probe Resource state is invalid")
        return revision, resource_count


def build_probe_adapter() -> MCPServerAdapter:
    descriptor = next(
        descriptor
        for descriptor in build_server().as_mcp_adapter().list_tools()
        if descriptor.name == "aggregate_preview"
    )
    delay = float(os.getenv("MCP_PROBE_DELAY_SECONDS", "0"))
    if delay < 0 or delay > 5:
        raise ValueError("MCP_PROBE_DELAY_SECONDS 必须在 0 到 5 秒之间")

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        if delay:
            time.sleep(delay)
        return aggregate_preview(arguments)

    state_path = os.getenv("MCP_PROBE_RESOURCE_STATE", "").strip()
    provider = (
        ProbeResourceProvider(
            Path(state_path),
            project_id=os.getenv("MCP_PROBE_PROJECT_ID", "probe-project"),
            subject_id=os.getenv("MCP_PROBE_SUBJECT_ID", "probe-user"),
        )
        if state_path
        else None
    )
    return MCPServerAdapter(
        "dataset-ops-probe",
        [MCPToolBinding(descriptor=descriptor, handler=handler)],
        resource_provider=provider,
    )


if __name__ == "__main__":
    run_adapter(build_probe_adapter(), default_port=8106)
