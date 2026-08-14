"""v2.4 stage-1 policy, audit and trace skeleton tests."""

from __future__ import annotations

from typing import Any

import pytest
from packages.governance.audit import AuditEvent
from packages.governance.observability import trace_span
from packages.governance.permissions import Principal
from packages.governance.policy import ToolPolicyGateway, ToolPolicyRequest

_DATASET_REF = "a" * 32


def _request(**overrides: Any) -> ToolPolicyRequest:
    values: dict[str, Any] = {
        "principal": Principal("user-1", "tenant-1"),
        "project_id": "project-1",
        "conversation_id": "conversation-1",
        "run_id": "run-1",
        "tool_name": "aggregate_preview",
        "arguments": {"dataset_ref": _DATASET_REF, "group_col": "地区"},
        "calls_used": 0,
        "max_tool_calls": 4,
        "resource_project_id": "project-1",
    }
    values.update(overrides)
    return ToolPolicyRequest(**values)


def test_policy_allows_static_tool_and_audits_only_argument_hash() -> None:
    events: list[AuditEvent] = []
    gateway = ToolPolicyGateway(audit_recorder=events.append)

    decision = gateway.authorize(_request())

    assert decision.allowed is True
    assert decision.code == "policy_allowed"
    assert len(decision.arguments_hash) == 64
    assert events[0].outcome == "allowed"
    serialized = events[0].to_dict()
    assert serialized["detail"]["arguments_hash"] == decision.arguments_hash
    assert "arguments" not in serialized["detail"]
    assert _DATASET_REF not in str(serialized)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"tool_name": "code_interpreter"}, "tool_not_allowlisted"),
        ({"resource_project_id": "project-2"}, "cross_project_resource_denied"),
        ({"calls_used": 4}, "tool_budget_exhausted"),
        ({"run_id": ""}, "invalid_policy_context"),
    ],
)
def test_policy_denies_before_execution(
    overrides: dict[str, Any], code: str
) -> None:
    events: list[AuditEvent] = []
    decision = ToolPolicyGateway(audit_recorder=events.append).authorize(
        _request(**overrides)
    )

    assert decision.allowed is False
    assert decision.code == code
    assert events[0].outcome == "denied"


@pytest.mark.parametrize("tool_name", ["join_preflight", "join_datasets"])
def test_policy_authorizes_every_join_dataset_reference(tool_name: str) -> None:
    arguments = {
        "left_dataset_ref": "a" * 32,
        "right_dataset_ref": "b" * 32,
        "left_key": "id",
        "right_key": "id",
        "join_type": "inner",
    }
    gateway = ToolPolicyGateway(audit_recorder=lambda _event: None)

    allowed = gateway.authorize(
        _request(
            tool_name=tool_name,
            arguments=arguments,
            resource_project_id=None,
            resource_project_ids=("project-1", "project-1"),
        )
    )
    foreign_right = gateway.authorize(
        _request(
            tool_name=tool_name,
            arguments=arguments,
            resource_project_id=None,
            resource_project_ids=("project-1", "project-2"),
        )
    )
    missing_right = gateway.authorize(
        _request(
            tool_name=tool_name,
            arguments=arguments,
            resource_project_id=None,
            resource_project_ids=("project-1", None),
        )
    )

    assert allowed.allowed is True
    assert foreign_right.code == "cross_project_resource_denied"
    assert missing_right.code == "unregistered_resource_denied"


def test_trace_records_completion_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Logger:
        def info(self, event: str, **attrs: object) -> None:
            calls.append((event, attrs))

        def error(self, event: str, **attrs: object) -> None:
            calls.append((event, attrs))

    monkeypatch.setattr("packages.governance.observability._log", _Logger())
    with trace_span("tool.execute", trace_id="trace-1", tool="safe") as span:
        span.set_attributes(result_type="dict")

    with pytest.raises(RuntimeError, match="failed"):
        with trace_span("model.complete", trace_id="trace-2"):
            raise RuntimeError("failed")

    assert [item[0] for item in calls] == [
        "trace.started",
        "trace.completed",
        "trace.started",
        "trace.failed",
    ]
    assert calls[1][1]["result_type"] == "dict"
    assert calls[3][1]["error_type"] == "RuntimeError"
