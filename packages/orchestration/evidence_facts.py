"""工具计数的版本化投影；数值仍只有 Evidence.value_index 一个来源。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA = "operation-count-facts-v1"
OPERATIONS = ("exclude_row_indices", "filters", "drop_nulls", "drop_duplicates", "sort")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def seal_count_facts(
    *,
    summary: dict[str, Any],
    result: Any,
    tool: str,
    arguments: dict[str, Any],
    run_id: str,
    invocation_id: str,
    result_hash: str,
    branch_hash: str,
) -> dict[str, Any]:
    """成功事务从实际工具输出生成计数，不认证调用方提供的数值。"""
    if tool not in {"transform_dataset", "aggregate_preview"}:
        return summary
    result = result if isinstance(result, dict) else {}
    facts: list[dict[str, Any]] = []
    input_ref = arguments.get("dataset_ref")
    output_ref = result.get("dataset_ref") if tool == "transform_dataset" else input_ref
    operations = {key: arguments[key] for key in OPERATIONS if key in arguments}
    invalid = False
    count_state = "available"

    def add(
        path: str,
        value: Any,
        role: str,
        operation: str,
        basis: str,
        columns: Any,
        step: int | None = None,
    ) -> None:
        nonlocal invalid
        if not _count(value):
            invalid = True
            return
        facts.append(
            {
                "path": path,
                "value": str(value),
                "source_kind": "number",
                "fact": {
                    "role": role,
                    "operation": operation,
                    "basis": basis,
                    "columns": columns,
                    "step": step,
                },
            }
        )

    if tool == "transform_dataset":
        invalid = (
            not operations
            or result.get("parent_ref") != input_ref
            or result.get("transform") != operations
        )
        before, after = result.get("rows_before"), result.get("rows_after")
        if not _count(before) or not _count(after) or after > before:
            invalid = True
        operation = next(iter(operations)) if len(operations) == 1 else "transform"
        add("$.rows_before", result.get("rows_before"), "before", operation, "rows", None)
        add("$.rows_after", result.get("rows_after"), "after", operation, "rows", None)
        changes = result.get("row_changes")
        if changes is not None:
            if (
                not isinstance(changes, dict)
                or type(changes.get("schema_version")) is not int
                or changes.get("schema_version") != 1
            ):
                invalid = True
            else:
                basis = "duplicate_excess" if operation == "drop_duplicates" else "removed_rows"
                add(
                    "$.row_changes.removed_count",
                    changes.get("removed_count"),
                    "removed",
                    operation,
                    basis,
                    operations.get("drop_duplicates") if operation == "drop_duplicates" else None,
                )
                steps = changes.get("steps")
                if (
                    not isinstance(steps, list)
                    or not all(isinstance(s, dict) for s in steps)
                    or [s.get("operation") for s in steps] != list(operations)
                ):
                    invalid = True
                else:
                    previous = before
                    for i, step in enumerate(steps):
                        step_before, step_after, removed = (
                            step.get(key) for key in ("rows_before", "rows_after", "removed_count")
                        )
                        step_op = step["operation"]
                        basis = (
                            "duplicate_excess" if step_op == "drop_duplicates" else "removed_rows"
                        )
                        step_columns = (
                            operations.get(step_op)
                            if step_op in {"drop_duplicates", "drop_nulls"}
                            else None
                        )
                        if (
                            not all(_count(n) for n in (step_before, step_after, removed))
                            or step_before != previous
                            or step_before - step_after != removed
                            or step.get("count_basis") != basis
                            or step.get("columns") not in (None, step_columns)
                            or (step_op == "sort" and removed != 0)
                        ):
                            invalid = True
                        previous = step_after
                        if len(steps) == 1:
                            continue  # The top-level facts already represent this step.
                        for field, role in (
                            ("rows_before", "before"),
                            ("rows_after", "after"),
                            ("removed_count", "removed"),
                        ):
                            add(
                                f"$.row_changes.steps[{i}].{field}",
                                step.get(field),
                                role,
                                step["operation"],
                                basis if role == "removed" else "rows",
                                step_columns,
                                i,
                            )
                    if (
                        previous != after
                        or not _count(before)
                        or not _count(after)
                        or changes.get("removed_count") != before - after
                    ):
                        invalid = True
    else:
        rows, group_total, truncated = (
            result.get(key) for key in ("rows", "group_total", "truncated")
        )
        required = {"rows", "group_total", "truncated", "group_col", "value_col", "agg"}
        if not required.issubset(result) or not {"dataset_ref", "group_col", "agg"}.issubset(
            arguments
        ):
            count_state = "unavailable"  # Partial legacy results certify no group count.
        invalid = (
            ("rows" in result and not isinstance(rows, list))
            or ("group_total" in result and not _count(group_total))
            or ("truncated" in result and type(truncated) is not bool)
            or (
                isinstance(rows, list)
                and _count(group_total)
                and (
                    group_total < len(rows)
                    or (type(truncated) is bool and truncated != (group_total > len(rows)))
                )
            )
            or any(
                result[key] != arguments[key]
                for key in ("group_col", "value_col", "agg")
                if key in result and key in arguments
            )
        )
        if count_state == "available":
            add(
                "$.group_total",
                group_total,
                "group_total",
                "aggregate",
                "groups",
                [arguments.get("group_col")],
            )
    # Transform's other numeric metadata is not a quantitative fact. Aggregate
    # measurements retain their existing typed paths; only group_total migrates.
    old = (
        []
        if tool == "transform_dataset"
        else [
            item for item in summary.get("value_index", []) if item.get("path") != "$.group_total"
        ]
    )
    projection = {
        "schema": SCHEMA,
        "producer": tool,
        "run_id": run_id,
        "invocation_id": invocation_id,
        "result_hash": result_hash,
        "branch_hash": branch_hash,
        "input_ref": input_ref,
        "output_ref": output_ref,
        "columns": result.get("columns", []),
        "step_count": len(operations),
        "arguments": arguments,
        "invalid": invalid,
        "count_state": count_state,
        "paths": [item["path"] for item in facts],
    }
    projection["projection_hash"] = _hash({"envelope": projection, "facts": facts})
    return {
        **summary,
        "numeric_context": {**arguments, "data_version_hash": branch_hash},
        "value_index": old + facts,
        "fact_projection": projection,
    }


def valid_count_projection(
    summary: dict[str, Any], *, tool: str, run_id: str, invocation_id: str, result_hash: str
) -> bool:
    """核对持久投影，不假定完整原始工具结果仍被保存。"""
    projection = summary.get("fact_projection")
    if (
        not isinstance(projection, dict)
        or projection.get("schema") != SCHEMA
        or projection.get("invalid")
    ):
        return False
    context = summary.get("numeric_context")
    arguments = projection.get("arguments")
    if (
        not isinstance(context, dict)
        or not isinstance(arguments, dict)
        or context != {**arguments, "data_version_hash": projection.get("branch_hash")}
    ):
        return False
    if any(
        projection.get(key) != value
        for key, value in {
            "producer": tool,
            "run_id": run_id,
            "invocation_id": invocation_id,
            "result_hash": result_hash,
        }.items()
    ):
        return False
    paths = projection.get("paths")
    if (
        not isinstance(paths, list)
        or (
            not paths
            and not (tool == "aggregate_preview" and projection.get("count_state") == "unavailable")
        )
        or not all(isinstance(path, str) for path in paths)
        or len(paths) != len(set(paths))
    ):
        return False
    index = summary.get("value_index")
    if not isinstance(index, list) or not all(isinstance(item, dict) for item in index):
        return False
    facts = [item for item in summary.get("value_index", []) if item.get("path") in paths]
    if any(
        item.get("path") not in paths and (tool == "transform_dataset" or "fact" in item)
        for item in summary.get("value_index", [])
    ):
        return False
    if [item.get("path") for item in facts] != paths:
        return False
    envelope = {k: v for k, v in projection.items() if k != "projection_hash"}
    return projection.get("projection_hash") == _hash({"envelope": envelope, "facts": facts})
