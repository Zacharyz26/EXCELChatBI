"""R1 补充独立反例：生产计数自洽与聚合结果绑定，不更改旧期待。"""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from mcp_servers.dataset_ops.tools import aggregate_preview, transform_dataset
from packages.session.task_models import EvidenceRecord, ToolInvocation

# 支持独立目录和复制进 tests/ 后的同名 helper；不修改产品路径或实现。
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tests.test_r1_producer_projection import (  # noqa: E402
    Workspace,
    _forbid_network,
    check_answer,
    projection_digest,
    workspace,
)

__all__ = ["workspace", "_forbid_network"]


def commit_result(
    ws: Workspace,
    tool: str,
    args: dict[str, Any],
    result: dict[str, Any],
    summary: dict[str, Any] | None = None,
) -> tuple[ToolInvocation, EvidenceRecord]:
    """真实调用结果或明确标注的变异，经过相同成功事务封存。"""
    ws.commits += 1
    invocation, _ = ws.tasks.start_invocation(
        run_id=ws.run.run_id,
        tool_call_id=f"integrity-{ws.commits}",
        tool_name=tool,
        arguments=args,
        idempotency_key=f"integrity-{ws.commits}",
    )
    if tool == "transform_dataset":
        ws.session.register_dataset(
            ref=result["dataset_ref"],
            project_id=ws.project_id,
            filename="synthetic-integrity.csv",
            profile={"columns": [{"name": key} for key in result["columns"]]},
            parent_ref=result["parent_ref"],
            transform=result["transform"],
        )
    supplied = (
        summary
        if summary is not None
        else build_evidence_summary(
            summary="独立计数自洽验证", result=result, artifact_id=None, arguments=args
        )
    )
    ws.run, completed, evidence, _, _, _ = ws.tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=ws.run.state_version,
        assistant_message_id=ws.assistant_id,
        result=result,
        evidence_kind="tool_result",
        evidence_source={"tool": tool, "dataset_ref": args["dataset_ref"]},
        evidence_summary=supplied,
        artifact_draft=None,
    )
    return completed, evidence


def assert_rejected(
    ws: Workspace, tool: str, args: dict[str, Any], result: dict[str, Any], text: str
) -> None:
    """允许明确在成功事务前拒绝，或持久 unknown 后由完整 Verifier 拒绝。"""
    try:
        invocation, evidence = commit_result(ws, tool, args, result)
    except ValueError:
        assert ws.tasks.list_evidence(ws.run.run_id) == []
        assert ws.tasks.list_invocations(ws.run.run_id)[0].status != "succeeded"
        return
    verdict = check_answer(ws, text, invocation, evidence)
    assert not verdict.passed, (text, result, evidence.summary, verdict)


@pytest.mark.parametrize(
    "mutation",
    [
        "total_delta",
        "output_rows",
        "first_input",
        "last_output",
        "disconnected_steps",
        "step_delta",
        "negative_step",
        "missing_step_delta",
        "missing_step_basis",
        "wrong_step_basis",
        "wrong_dedup_columns",
        "missing_steps",
        "reordered_steps",
        "version_boolean",
        "version_unknown",
        "null_changes",
        "missing_changes",
    ],
)
def test_transform_rejects_malformed_step_and_total_contract(
    workspace: Workspace, mutation: str
) -> None:
    args = {
        "dataset_ref": workspace.source_ref,
        "filters": [{"column": "频道", "op": "==", "value": "A"}],
        "drop_nulls": ["时长"],
        "drop_duplicates": [],
        "sort": [{"column": "时长", "order": "desc"}],
    }
    result = transform_dataset(args)
    assert (result["rows_before"], result["rows_after"]) == (5, 2)
    assert result["row_changes"]["removed_count"] == 3
    changes = result["row_changes"]
    steps = changes["steps"]
    text = "删除3行。"
    if mutation == "total_delta":
        changes["removed_count"] = 2
        text = "删除2行。"
    elif mutation == "output_rows":
        result["rows_after"] = 3
    elif mutation == "first_input":
        steps[0]["rows_before"] = 6
    elif mutation == "last_output":
        steps[-1]["rows_after"] = 1
    elif mutation == "disconnected_steps":
        steps[1]["rows_before"] = 5
        steps[1]["removed_count"] = 2
    elif mutation == "step_delta":
        steps[0]["removed_count"] = 2
    elif mutation == "negative_step":
        steps[1]["removed_count"] = -1
    elif mutation == "missing_step_delta":
        del steps[1]["removed_count"]
    elif mutation == "missing_step_basis":
        del steps[2]["count_basis"]
    elif mutation == "wrong_step_basis":
        steps[2]["count_basis"] = "removed_rows"
    elif mutation == "wrong_dedup_columns":
        steps[2]["columns"] = ["频道"]
    elif mutation == "missing_steps":
        del changes["steps"]
    elif mutation == "reordered_steps":
        changes["steps"] = list(reversed(steps))
    elif mutation == "version_boolean":
        changes["schema_version"] = True
    elif mutation == "version_unknown":
        changes["schema_version"] = 999
    elif mutation == "null_changes":
        result["row_changes"] = None
    elif mutation == "missing_changes":
        del result["row_changes"]
    assert_rejected(workspace, "transform_dataset", args, result, text)


@pytest.mark.parametrize(
    "mutation",
    [
        "rows_before",
        "rows_after",
        "removed_count",
        "missing_count",
        "negative_count",
        "bool_count",
        "wrong_basis",
        "wrong_columns",
        "null_step",
        "extra_step",
    ],
)
def test_single_operation_steps_are_not_skipped_by_sealer(
    workspace: Workspace, mutation: str
) -> None:
    args = {"dataset_ref": workspace.source_ref, "drop_duplicates": []}
    result = transform_dataset(args)
    assert result["row_changes"]["removed_count"] == 1
    steps = result["row_changes"]["steps"]
    step = steps[0]
    if mutation in {"rows_before", "rows_after", "removed_count"}:
        step[mutation] = 99
    elif mutation == "missing_count":
        del step["removed_count"]
    elif mutation == "negative_count":
        step["removed_count"] = -1
    elif mutation == "bool_count":
        step["removed_count"] = True
    elif mutation == "wrong_basis":
        step["count_basis"] = "participating_rows"
    elif mutation == "wrong_columns":
        step["columns"] = ["频道"]
    elif mutation == "null_step":
        steps[0] = None
    elif mutation == "extra_step":
        steps.append(None)
    assert_rejected(workspace, "transform_dataset", args, result, "去重删除1行。")


def aggregate_arguments(ws: Workspace, *, limit: int = 20) -> dict[str, Any]:
    return {
        "dataset_ref": ws.source_ref,
        "group_col": "频道",
        "value_col": "时长",
        "agg": "sum",
        "sort": "group",
        "limit": limit,
    }


@pytest.mark.parametrize("limit", [1, 20])
def test_aggregate_projection_uses_all_groups_and_real_source(
    workspace: Workspace, limit: int
) -> None:
    args = aggregate_arguments(workspace, limit=limit)
    result = aggregate_preview(args)
    # Independent five-row oracle: A: 10+10+20=40, B:30, two groups, total70.
    assert result["group_total"] == 2
    assert result["total_value"] == 70
    assert len(result["rows"]) == min(limit, 2)
    assert result["truncated"] is (limit == 1)
    forged = build_evidence_summary(
        summary="aggregate", result=result, artifact_id=None, arguments=args
    )
    for item in forged["value_index"]:
        if item["path"] == "$.group_total":
            item["value"] = "999"
    invocation, evidence = commit_result(workspace, "aggregate_preview", args, result, forged)
    fact = next(item for item in evidence.summary["value_index"] if item["path"] == "$.group_total")
    assert fact["value"] == "2"
    assert evidence.summary["fact_projection"]["projection_hash"] == projection_digest(evidence)
    assert check_answer(workspace, "共有两个频道。", invocation, evidence).passed
    assert not check_answer(workspace, "共有三个频道。", invocation, evidence).passed
    assert not check_answer(workspace, "共有两个时长。", invocation, evidence).passed
    assert not check_answer(workspace, "共有999个频道。", invocation, evidence).passed


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_group_total",
        "null_group_total",
        "bool_group_total",
        "negative_group_total",
        "group_total_too_small",
        "group_total_too_large",
        "false_truncation",
        "missing_rows",
        "wrong_group_col",
        "wrong_value_col",
        "wrong_aggregation",
    ],
)
def test_aggregate_rejects_inconsistent_source_and_group_count(
    workspace: Workspace, mutation: str
) -> None:
    args = aggregate_arguments(workspace)
    result = aggregate_preview(args)
    text = "共有两个频道。"
    if mutation == "missing_group_total":
        del result["group_total"]
    elif mutation == "null_group_total":
        result["group_total"] = None
    elif mutation == "bool_group_total":
        result["group_total"] = True
        text = "共有一个频道。"
    elif mutation == "negative_group_total":
        result["group_total"] = -1
    elif mutation == "group_total_too_small":
        result["group_total"] = 1
        text = "共有一个频道。"
    elif mutation == "group_total_too_large":
        result["group_total"] = 3
        text = "共有三个频道。"
    elif mutation == "false_truncation":
        result["truncated"] = True
    elif mutation == "missing_rows":
        del result["rows"]
    elif mutation == "wrong_group_col":
        result["group_col"] = "备注"
    elif mutation == "wrong_value_col":
        result["value_col"] = "频道"
    elif mutation == "wrong_aggregation":
        result["agg"] = "count"
    assert_rejected(workspace, "aggregate_preview", args, result, text)


@pytest.mark.parametrize("legacy_form", ["missing", "null"])
def test_legacy_without_delta_never_borrows_input_output_values(
    workspace: Workspace, legacy_form: str
) -> None:
    args = {"dataset_ref": workspace.source_ref, "drop_duplicates": []}
    result = transform_dataset(args)
    if legacy_form == "missing":
        del result["row_changes"]
    else:
        result["row_changes"] = None
    invocation, evidence = commit_result(workspace, "transform_dataset", args, result)
    assert not check_answer(workspace, "删除1行。", invocation, evidence).passed
    assert not check_answer(workspace, "删除4行。", invocation, evidence).passed


def test_explicit_null_projection_is_not_legacy_even_without_numeric_text(
    workspace: Workspace,
) -> None:
    args = {"dataset_ref": workspace.source_ref, "drop_duplicates": []}
    result = transform_dataset(args)
    invocation, evidence = commit_result(workspace, "transform_dataset", args, result)
    summary = copy.deepcopy(evidence.summary)
    summary["fact_projection"] = None
    evidence = replace(evidence, summary=summary)
    verdict = check_answer(workspace, "已完成所请求的处理。", invocation, evidence)
    assert not verdict.passed, "显式未知投影不能被当作未迁移legacy而允许完成"
    assert any(issue.code == "invalid_fact_projection" for issue in verdict.issues)
