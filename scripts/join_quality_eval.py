"""v2.5 6E-4 anonymous governed-Join quality gate.

The frozen cases contain only domain-neutral generators. The gate executes the
real deterministic Join preflight and selected materializations, while its
report excludes keys, values, column names, Dataset references and row counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.common.catalog import tool_output_schema  # noqa: E402
from mcp_servers.common.contracts import MCPProtocolError, validate_json  # noqa: E402
from mcp_servers.dataset_ops.tools import (  # noqa: E402
    join_datasets as _join_datasets,
)
from mcp_servers.dataset_ops.tools import join_preflight as _join_preflight  # noqa: E402
from packages.common.dataset_store import (  # noqa: E402
    delete_dataset,
    save_dataframe,
    save_metadata,
)

DEFAULT_CASES = Path(__file__).parent / "join_quality_eval_set.jsonl"
THRESHOLDS: dict[str, float | int] = {
    "expected_outcome_rate": 1.0,
    "output_contract_rate": 1.0,
    "risk_classification_rate": 1.0,
    "read_only_preflight_rate": 1.0,
    "failure_closed_rate": 1.0,
    "execution_contract_rate": 1.0,
    "two_parent_result_rate": 1.0,
    "bounded_join_violations": 0,
}

_CASE_KEYS = frozenset(
    {"id", "left", "right", "join_type", "setup", "protection", "execute", "expected"}
)
_SIDE_KEYS = frozenset({"kind", "start", "unique", "repeats", "nulls"})
_EXPECTED_KEYS = frozenset({"status", "relationship", "risk_codes", "error_guard"})
_STATUSES = frozenset({"ready", "requires_confirmation", "blocked", "error"})
_RELATIONSHIPS = frozenset(
    {"one_to_one", "one_to_many", "many_to_one", "many_to_many", "no_matches", "incompatible"}
)
_RISK_CODES = frozenset(
    {
        "incompatible_key_types",
        "no_matching_keys",
        "output_row_limit",
        "many_to_many",
        "row_expansion",
        "left_null_keys",
        "right_null_keys",
    }
)
_ERROR_MARKERS = {
    "protected_key": "关联键受数据策略保护",
    "distinct_datasets": "必须是两个不同的数据集",
}


def preflight_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    """Public seam for exercising and contract-drift testing the real preflight."""
    return _join_preflight(arguments)


def execute_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    """Public seam for selected deterministic materialization checks."""
    return _join_datasets(arguments)


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """Load the frozen strict JSONL contract and reject ambiguous fixtures."""
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict) or set(raw) != _CASE_KEYS:
            raise ValueError(f"第 {line_number} 行字段集合无效")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"第 {line_number} 行缺少 case id")
        if case_id in seen:
            raise ValueError(f"case id 重复: {case_id}")
        seen.add(case_id)
        _validate_case(raw, case_id)
        cases.append(raw)
    if not cases:
        raise ValueError("Join 质量评测用例为空")
    return cases


def _validate_case(case: dict[str, Any], case_id: str) -> None:
    for side_name in ("left", "right"):
        side = case.get(side_name)
        if not isinstance(side, dict) or set(side) != _SIDE_KEYS:
            raise ValueError(f"{case_id}: {side_name} 生成器契约无效")
        if side.get("kind") not in {"number", "text"}:
            raise ValueError(f"{case_id}: {side_name}.kind 无效")
        for key in ("start", "unique", "repeats", "nulls"):
            value = side.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{case_id}: {side_name}.{key} 无效")
        if (
            int(side["start"]) < 0
            or not 1 <= int(side["unique"]) <= 100
            or not 1 <= int(side["repeats"]) <= 1_000
            or not 0 <= int(side["nulls"]) <= 10
        ):
            raise ValueError(f"{case_id}: {side_name} 生成器范围无效")
    if case.get("join_type") not in {"inner", "left", "right", "full"}:
        raise ValueError(f"{case_id}: join_type 无效")
    if case.get("setup") not in {"distinct", "same_dataset"}:
        raise ValueError(f"{case_id}: setup 无效")
    if case.get("protection") not in {"none", "left_mask", "right_exclude"}:
        raise ValueError(f"{case_id}: protection 无效")
    if not isinstance(case.get("execute"), bool):
        raise ValueError(f"{case_id}: execute 无效")
    expected = case.get("expected")
    if not isinstance(expected, dict) or set(expected) != _EXPECTED_KEYS:
        raise ValueError(f"{case_id}: expected 契约无效")
    status = expected.get("status")
    relationship = expected.get("relationship")
    risks = expected.get("risk_codes")
    error_guard = expected.get("error_guard")
    if status not in _STATUSES:
        raise ValueError(f"{case_id}: expected.status 无效")
    if (
        not isinstance(risks, list)
        or any(not isinstance(item, str) for item in risks)
        or len(risks) != len(set(cast(list[str], risks)))
        or any(item not in _RISK_CODES for item in risks)
    ):
        raise ValueError(f"{case_id}: expected.risk_codes 无效")
    if status == "error":
        if relationship is not None or risks or error_guard not in _ERROR_MARKERS:
            raise ValueError(f"{case_id}: error expectation 无效")
    elif relationship not in _RELATIONSHIPS or error_guard is not None:
        raise ValueError(f"{case_id}: preflight expectation 无效")
    if case["execute"] and status not in {"ready", "requires_confirmation"}:
        raise ValueError(f"{case_id}: blocked/error 场景不能请求执行")


def run_evaluation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Run real deterministic Join behavior and emit a data-minimized report."""
    rows = [_evaluate_case(case) for case in cases]
    preflight_rows = [row for row in rows if row["expected_status"] != "error"]
    guarded_rows = [
        row for row in rows if row["expected_status"] in {"blocked", "error"}
    ]
    execution_rows = [row for row in rows if row["execution_requested"]]

    def rate(items: list[dict[str, Any]], key: str) -> float:
        return sum(bool(item[key]) for item in items) / len(items) if items else 1.0

    metrics: dict[str, float | int] = {
        "expected_outcome_rate": rate(rows, "outcome_matches"),
        "output_contract_rate": rate(preflight_rows, "output_contract"),
        "risk_classification_rate": rate(preflight_rows, "risk_classification"),
        "read_only_preflight_rate": rate(preflight_rows, "read_only_preflight"),
        "failure_closed_rate": rate(guarded_rows, "failure_closed"),
        "execution_contract_rate": rate(execution_rows, "execution_contract"),
        "two_parent_result_rate": rate(execution_rows, "two_parent_result"),
        "bounded_join_violations": sum(
            int(row["bounded_join_violation"]) for row in rows
        ),
    }
    misses = {
        name: {"actual": metrics[name], "required": required}
        for name, required in THRESHOLDS.items()
        if metrics[name] != required
    }
    return {
        "evaluation": "v2.5_governed_join",
        "case_set_sha256": _stable_hash(cases),
        "case_count": len(rows),
        "preflight_case_count": len(preflight_rows),
        "guarded_case_count": len(guarded_rows),
        "execution_case_count": len(execution_rows),
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses and all(row["passed"] for row in rows),
        "misses": misses,
        "contains_key_values": False,
        "contains_column_names": False,
        "contains_dataset_refs": False,
        "contains_row_counts": False,
        "contains_joined_rows": False,
        "reads_user_raw_rows": False,
        "synthetic_generators_only": True,
        "preflight_tool_calls": len(rows),
        "execution_tool_calls": len(execution_rows),
        "model_calls": 0,
    }


def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    expected = cast(dict[str, Any], case["expected"])
    left_ref = save_dataframe(_frame_for(cast(dict[str, Any], case["left"]), "left"))
    right_ref = left_ref
    if case["setup"] == "distinct":
        right_ref = save_dataframe(_frame_for(cast(dict[str, Any], case["right"]), "right"))
    created_refs = {left_ref, right_ref}
    if case["protection"] == "left_mask":
        save_metadata(left_ref, {"policy": {"columns": {"left_key": "mask"}}})
    elif case["protection"] == "right_exclude":
        save_metadata(right_ref, {"policy": {"columns": {"right_key": "exclude"}}})

    arguments = {
        "left_dataset_ref": left_ref,
        "right_dataset_ref": right_ref,
        "left_key": "left_key",
        "right_key": "right_key",
        "join_type": case["join_type"],
    }
    result: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    error_text: str | None = None
    try:
        result = preflight_tool(arguments)
        if case["execute"]:
            execution = execute_tool(arguments)
            output_ref = execution.get("dataset_ref")
            if isinstance(output_ref, str):
                created_refs.add(output_ref)
    except ValueError as exc:
        error_text = str(exc)

    expected_status = str(expected["status"])
    actual_status = str(result.get("status")) if result is not None else "error"
    output_contract = False
    relationship: str | None = None
    risk_codes: list[str] = []
    read_only_preflight = False
    bounded_join_violation = False
    if result is not None:
        try:
            validate_json(
                result,
                tool_output_schema("join_preflight"),
                code="invalid_tool_output",
                label="Join 质量门禁预检输出",
            )
            output_contract = True
        except MCPProtocolError:
            output_contract = False
        raw_relationship = result.get("relationship")
        relationship = raw_relationship if isinstance(raw_relationship, str) else None
        raw_risks = result.get("risks")
        if isinstance(raw_risks, list):
            risk_codes = [
                str(item.get("code"))
                for item in raw_risks
                if isinstance(item, dict) and isinstance(item.get("code"), str)
            ]
        read_only_preflight = (
            result.get("mutates_data") is False
            and result.get("raw_rows_returned") is False
        )
        bounded_join_violation = (
            actual_status not in _STATUSES - {"error"}
            or relationship not in _RELATIONSHIPS
            or any(code not in _RISK_CODES for code in risk_codes)
        )

    risk_classification = (
        relationship == expected["relationship"]
        and risk_codes == expected["risk_codes"]
    )
    error_guard = expected.get("error_guard")
    marker = _ERROR_MARKERS.get(str(error_guard)) if error_guard is not None else None
    failure_closed = (
        expected_status not in {"blocked", "error"}
        or (
            expected_status == "blocked"
            and result is not None
            and result.get("executable") is False
            and result.get("requires_confirmation") is False
            and any(
                isinstance(item, dict) and item.get("severity") == "blocking"
                for item in cast(list[object], result.get("risks", []))
            )
            and execution is None
        )
        or (
            expected_status == "error"
            and result is None
            and error_text is not None
            and marker is not None
            and marker in error_text
            and execution is None
        )
    )

    execution_contract = not bool(case["execute"])
    two_parent_result = not bool(case["execute"])
    if execution is not None:
        try:
            validate_json(
                execution,
                tool_output_schema("join_datasets"),
                code="invalid_tool_output",
                label="Join 质量门禁执行输出",
            )
            execution_contract = (
                result is not None
                and execution.get("schema") == "chatbi-join-result-v1"
                and execution.get("rows") == result.get("estimated_output_rows")
                and execution.get("preflight_status") == expected_status
                and execution.get("mutates_data") is True
                and execution.get("raw_rows_returned") is False
            )
        except MCPProtocolError:
            execution_contract = False
        two_parent_result = execution.get("parent_refs") == [left_ref, right_ref]

    outcome_matches = (
        actual_status == expected_status
        and (
            expected_status == "error"
            or (relationship == expected["relationship"] and risk_codes == expected["risk_codes"])
        )
    )
    checks = {
        "outcome_matches": outcome_matches,
        "output_contract": output_contract if expected_status != "error" else True,
        "risk_classification": risk_classification if expected_status != "error" else True,
        "read_only_preflight": read_only_preflight if expected_status != "error" else True,
        "failure_closed": failure_closed,
        "execution_contract": execution_contract,
        "two_parent_result": two_parent_result,
        "bounded_join_violation": bounded_join_violation,
    }
    for dataset_ref in created_refs:
        delete_dataset(dataset_ref)
    return {
        "id": case_id,
        "expected_status": expected_status,
        "actual_status": actual_status,
        "relationship": relationship,
        "risk_codes": risk_codes,
        "execution_requested": bool(case["execute"]),
        **checks,
        "passed": (
            all(value for key, value in checks.items() if key != "bounded_join_violation")
            and not bounded_join_violation
        ),
    }


def _frame_for(side: dict[str, Any], label: str) -> pd.DataFrame:
    values: list[object] = []
    for offset in range(int(side["unique"])):
        value: object = int(side["start"]) + offset
        if side["kind"] == "text":
            value = f"token-{value}"
        values.extend([value] * int(side["repeats"]))
    values.extend([None] * int(side["nulls"]))
    return pd.DataFrame(
        {
            f"{label}_key": values,
            f"{label}_signal": list(range(len(values))),
        }
    )


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _print_human(report: dict[str, Any]) -> None:
    print(f"受治理 Join：{report['case_count']} 个匿名合成场景")
    metrics = cast(dict[str, float | int], report["metrics"])
    for name in THRESHOLDS:
        value = metrics[name]
        print(f"- {name}: {value:.1%}" if isinstance(value, float) else f"- {name}: {value}")
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 6E-4 受治理 Join 质量门禁")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--json-output")
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print(f"受治理 Join 用例契约有效：{len(cases)} cases")
        return 0
    report = run_evaluation(cases)
    _print_human(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 报告：{output}")
    if args.enforce and not report["passed"]:
        print(f"受治理 Join 门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("受治理 Join 门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
