"""Static v2.4 service topology for the model-facing Agent tool catalog."""

from __future__ import annotations

from collections.abc import Mapping

AGENT_MCP_SERVICE_TOOLS: dict[str, tuple[str, ...]] = {
    "data-tools": (
        "get_data_profile",
        "transform_dataset",
        "aggregate_preview",
        "join_preflight",
        "join_datasets",
    ),
    "stats-tools": (
        "trend_analysis",
        "anomaly_detect",
        "regression",
        "correlation",
        "dimension_contribution",
        "group_compare",
        "forecast",
    ),
    "chart-tools": (
        "gen_chart",
        "chart_screenshot",
    ),
    "report-tools": ("generate_report",),
    "knowledge-tools": (
        "kb_search",
        "domain_definition_lookup",
    ),
}

AGENT_MCP_SERVICES = tuple(AGENT_MCP_SERVICE_TOOLS)
AGENT_MCP_TOOL_SERVICE = {
    tool_name: service_name
    for service_name, tool_names in AGENT_MCP_SERVICE_TOOLS.items()
    for tool_name in tool_names
}

AGENT_CAPABILITY_PROFILES = frozenset({"browser", "forecast", "gpu", "stats"})
DEFAULT_AGENT_CAPABILITY_PROFILES = frozenset({"browser", "stats"})


def parse_capability_profiles(raw: str) -> frozenset[str]:
    """Parse an exact, duplicate-free deployment profile allowlist."""
    if not raw.strip():
        return frozenset()
    items = tuple(item.strip() for item in raw.split(","))
    if any(not item for item in items):
        raise ValueError("Agent capability profile 不能包含空名称")
    if len(set(items)) != len(items):
        raise ValueError("Agent capability profile 不能重复")
    unknown = set(items) - AGENT_CAPABILITY_PROFILES
    if unknown:
        raise ValueError(
            "未知 Agent capability profile: " + ", ".join(sorted(unknown))
        )
    return frozenset(items)


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
