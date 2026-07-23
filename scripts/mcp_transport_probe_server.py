"""Probe-only MCP server exposing the canonical aggregate_preview binding."""

from __future__ import annotations

import os
import time

from mcp_servers.common.adapter import MCPServerAdapter, MCPToolBinding
from mcp_servers.common.sdk_adapter import run_adapter
from mcp_servers.dataset_ops.server import build_server
from mcp_servers.dataset_ops.tools import aggregate_preview


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

    return MCPServerAdapter(
        "dataset-ops-probe",
        [MCPToolBinding(descriptor=descriptor, handler=handler)],
    )


if __name__ == "__main__":
    run_adapter(build_probe_adapter(), default_port=8106)
