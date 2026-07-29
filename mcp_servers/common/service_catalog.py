"""Static v2.4 service topology for the model-facing Agent tool catalog."""

from __future__ import annotations

from collections.abc import Mapping

AGENT_MCP_SERVICE_TOOLS: dict[str, tuple[str, ...]] = {
    "data-tools": (
        "get_data_profile",
        "transform_dataset",
        "aggregate_preview",
    ),
    "stats-tools": (
        "trend_analysis",
        "anomaly_detect",
        "regression",
        "correlation",
    ),
    "chart-tools": (
        "gen_chart",
        "chart_screenshot",
    ),
    "report-tools": ("generate_report",),
    "knowledge-tools": ("kb_search",),
}

AGENT_MCP_SERVICES = tuple(AGENT_MCP_SERVICE_TOOLS)
AGENT_MCP_TOOL_SERVICE = {
    tool_name: service_name
    for service_name, tool_names in AGENT_MCP_SERVICE_TOOLS.items()
    for tool_name in tool_names
}


def validate_service_keys(
    values: Mapping[str, str],
    *,
    label: str,
) -> None:
    """Require the complete, exact static service allowlist."""
    actual = set(values)
    expected = set(AGENT_MCP_SERVICES)
    if actual == expected:
        return
    missing = ", ".join(sorted(expected - actual)) or "无"
    unexpected = ", ".join(sorted(actual - expected)) or "无"
    raise ValueError(f"{label}服务清单不完整（缺少: {missing}；未知: {unexpected}）")
