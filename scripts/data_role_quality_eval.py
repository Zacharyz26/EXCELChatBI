"""v2.5 6B-3 数据角色与质量建议代表性门禁。

评测只消费冻结的匿名 DataProfile 元数据，不读取原始行、不调用模型，
也不执行清洗。报告仅保留 case ID 和聚合计数，不回显列名或画像。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.excel_parser.advisor import (  # noqa: E402
    diagnose_data_quality as _diagnose_data_quality,
)
from mcp_servers.excel_parser.advisor import (  # noqa: E402
    infer_data_roles,
)
from mcp_servers.excel_parser.profile import ColumnProfile, DataProfile  # noqa: E402

diagnose_data_quality = _diagnose_data_quality

DEFAULT_CASES = Path(__file__).parent / "data_role_quality_eval_set.jsonl"
THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "role_accuracy": {"operator": ">=", "value": 0.95},
    "ambiguity_recall": {"operator": ">=", "value": 1.0},
    "high_confidence_error_rate": {"operator": "<=", "value": 0.0},
    "auto_mutation_violation_count": {"operator": "<=", "value": 0},
}

_ROLES = frozenset({"time", "metric", "dimension", "identifier", "unknown"})
_DTYPES = frozenset({"int", "float", "str", "datetime", "bool"})
_QUALITY_CODES = frozenset(
    {
        "duplicate_rows",
        "all_values_missing",
        "missing_values",
        "constant_column",
        "non_unique_identifier",
        "ambiguous_data_role",
    }
)
_CASE_KEYS = frozenset(
    {"id", "profile", "duplicate_rows", "expected_roles", "expected_quality_codes"}
)
_PROFILE_KEYS = frozenset({"row_count", "columns"})
_COLUMN_KEYS = frozenset({"name", "dtype", "null_ratio", "distinct_count"})
_ROLE_KEYS = frozenset({"primary_role", "ambiguous"})


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """加载冻结评测集，拒绝未知字段、重复 ID 和不自洽画像。"""
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
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
        if case_id in seen_ids:
            raise ValueError(f"case id 重复: {case_id}")
        seen_ids.add(case_id)
        _validate_case(raw, case_id)
        cases.append(raw)
    if not cases:
        raise ValueError("数据角色质量用例为空")
    return cases


def _validate_case(case: dict[str, Any], case_id: str) -> None:
    profile = case.get("profile")
    if not isinstance(profile, dict) or set(profile) != _PROFILE_KEYS:
        raise ValueError(f"{case_id}: profile 契约无效")
    row_count = profile.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 1:
        raise ValueError(f"{case_id}: row_count 无效")
    columns = profile.get("columns")
    if not isinstance(columns, list) or not 2 <= len(columns) <= 32:
        raise ValueError(f"{case_id}: columns 数量无效")
    column_names: list[str] = []
    for index, column in enumerate(columns):
        if not isinstance(column, dict) or set(column) != _COLUMN_KEYS:
            raise ValueError(f"{case_id}: 第 {index + 1} 列契约无效")
        name = column.get("name")
        dtype = column.get("dtype")
        null_ratio = column.get("null_ratio")
        distinct_count = column.get("distinct_count")
        if not isinstance(name, str) or not name.strip() or name in column_names:
            raise ValueError(f"{case_id}: 列名无效或重复")
        if dtype not in _DTYPES:
            raise ValueError(f"{case_id}: {name} dtype 无效")
        if (
            not isinstance(null_ratio, int | float)
            or isinstance(null_ratio, bool)
            or not 0.0 <= float(null_ratio) <= 1.0
        ):
            raise ValueError(f"{case_id}: {name} null_ratio 无效")
        non_null_count = round(row_count * (1.0 - float(null_ratio)))
        if (
            isinstance(distinct_count, bool)
            or not isinstance(distinct_count, int)
            or not 0 <= distinct_count <= non_null_count
        ):
            raise ValueError(f"{case_id}: {name} distinct_count 无效")
        column_names.append(name)

    duplicate_rows = case.get("duplicate_rows")
    if (
        isinstance(duplicate_rows, bool)
        or not isinstance(duplicate_rows, int)
        or not 0 <= duplicate_rows <= row_count
    ):
        raise ValueError(f"{case_id}: duplicate_rows 无效")
    expected_roles = case.get("expected_roles")
    if not isinstance(expected_roles, dict) or set(expected_roles) != set(column_names):
        raise ValueError(f"{case_id}: expected_roles 必须覆盖全部列")
    for name, expectation in expected_roles.items():
        if not isinstance(expectation, dict) or set(expectation) != _ROLE_KEYS:
            raise ValueError(f"{case_id}: {name} 角色期望契约无效")
        if expectation.get("primary_role") not in _ROLES:
            raise ValueError(f"{case_id}: {name} primary_role 无效")
        if not isinstance(expectation.get("ambiguous"), bool):
            raise ValueError(f"{case_id}: {name} ambiguous 必须是布尔值")
    quality_codes = case.get("expected_quality_codes")
    if (
        not isinstance(quality_codes, list)
        or any(not isinstance(code, str) or code not in _QUALITY_CODES for code in quality_codes)
    ):
        raise ValueError(f"{case_id}: expected_quality_codes 无效")


def run_evaluation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """通过真实确定性 advisor 运行冻结用例并生成脱敏报告。"""
    rows = [_evaluate_case(case) for case in cases]
    total_columns = sum(int(row["column_count"]) for row in rows)
    expected_ambiguous = sum(int(row["expected_ambiguous_count"]) for row in rows)
    role_correct = sum(int(row["role_correct_count"]) for row in rows)
    ambiguity_true_positive = sum(
        int(row["ambiguity_true_positive_count"]) for row in rows
    )
    high_confidence_errors = sum(
        int(row["high_confidence_error_count"]) for row in rows
    )
    mutation_violations = sum(int(row["auto_mutation_violation_count"]) for row in rows)
    metrics: dict[str, float | int] = {
        "role_accuracy": role_correct / total_columns,
        "ambiguity_recall": (
            ambiguity_true_positive / expected_ambiguous if expected_ambiguous else 1.0
        ),
        "high_confidence_error_rate": high_confidence_errors / total_columns,
        "auto_mutation_violation_count": mutation_violations,
    }
    misses = _threshold_misses(metrics)
    quality_failures = [row["id"] for row in rows if not row["quality_contract"]]
    if quality_failures:
        misses["quality_contract"] = {"failed_case_count": len(quality_failures)}
    return {
        "evaluation": "v2.5_data_role_quality",
        "case_set_sha256": hashlib.sha256(
            json.dumps(
                cases,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "case_count": len(rows),
        "column_count": total_columns,
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses,
        "misses": misses,
        "contains_column_names": False,
        "contains_profile_metadata": False,
        "reads_raw_rows": False,
    }


def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_from_case(case)
    before = json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True)
    roles = infer_data_roles(profile)
    quality = diagnose_data_quality(
        profile,
        roles,
        duplicate_rows=int(case["duplicate_rows"]),
    )
    after = json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True)
    role_items = cast(list[dict[str, Any]], roles["columns"])
    by_name = {str(item["column"]): item for item in role_items}
    expected_roles = cast(dict[str, dict[str, Any]], case["expected_roles"])
    role_correct = 0
    expected_ambiguous = 0
    ambiguity_true_positive = 0
    high_confidence_errors = 0
    for name, expected in expected_roles.items():
        actual = by_name[name]
        role_matches = actual.get("primary_role") == expected["primary_role"]
        role_correct += role_matches
        if bool(expected["ambiguous"]):
            expected_ambiguous += 1
            ambiguity_true_positive += bool(actual.get("ambiguous"))
        if (
            not role_matches
            and not bool(actual.get("ambiguous"))
            and float(actual.get("confidence", 0.0)) >= 0.75
        ):
            high_confidence_errors += 1
    actual_quality_codes = [
        str(item.get("code"))
        for item in cast(list[dict[str, Any]], quality.get("issues") or [])
    ]
    expected_quality_codes = cast(list[str], case["expected_quality_codes"])
    quality_contract = Counter(actual_quality_codes) == Counter(expected_quality_codes)
    recommendations = cast(
        list[Mapping[str, Any]], quality.get("recommendations") or []
    )
    mutation_violations = int(quality.get("mutates_data") is not False)
    mutation_violations += sum(
        recommendation.get("automatic") is not False
        for recommendation in recommendations
    )
    mutation_violations += int(before != after)
    row_passed = (
        high_confidence_errors == 0
        and ambiguity_true_positive == expected_ambiguous
        and quality_contract
        and mutation_violations == 0
    )
    return {
        "id": str(case["id"]),
        "column_count": len(role_items),
        "role_correct_count": role_correct,
        "expected_ambiguous_count": expected_ambiguous,
        "ambiguity_true_positive_count": ambiguity_true_positive,
        "high_confidence_error_count": high_confidence_errors,
        "quality_issue_count": len(actual_quality_codes),
        "quality_contract": quality_contract,
        "auto_mutation_violation_count": mutation_violations,
        "passed": row_passed,
    }


def _profile_from_case(case: dict[str, Any]) -> DataProfile:
    raw_profile = cast(dict[str, Any], case["profile"])
    raw_columns = cast(list[dict[str, Any]], raw_profile["columns"])
    columns = [
        ColumnProfile(
            name=str(column["name"]),
            dtype=str(column["dtype"]),
            null_ratio=float(column["null_ratio"]),
            distinct_count=int(column["distinct_count"]),
        )
        for column in raw_columns
    ]
    return DataProfile(
        dataset_ref=f"anonymous-{case['id']}",
        row_count=int(raw_profile["row_count"]),
        column_count=len(columns),
        columns=columns,
    )


def _threshold_misses(metrics: dict[str, float | int]) -> dict[str, object]:
    misses: dict[str, object] = {}
    for name, threshold in THRESHOLDS.items():
        actual = metrics[name]
        required = cast(float | int, threshold["value"])
        operator = threshold["operator"]
        if (operator == ">=" and actual < required) or (
            operator == "<=" and actual > required
        ):
            misses[name] = {
                "actual": actual,
                "operator": operator,
                "required": required,
            }
    return misses


def _print_human(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    print(
        f"数据角色/质量：{report['case_count']} 个匿名场景，"
        f"{report['column_count']} 列"
    )
    print(f"- role_accuracy: {metrics['role_accuracy']:.1%}")
    print(f"- ambiguity_recall: {metrics['ambiguity_recall']:.1%}")
    print(
        "- high_confidence_error_rate: "
        f"{metrics['high_confidence_error_rate']:.1%}"
    )
    print(
        "- auto_mutation_violation_count: "
        f"{metrics['auto_mutation_violation_count']}"
    )
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 6B-3 数据角色/质量门禁")
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
        print(f"数据角色/质量用例契约有效：{len(cases)} cases")
        return 0
    report = run_evaluation(cases)
    _print_human(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 报告：{output}")
    if args.enforce and not report["passed"]:
        print(f"数据角色/质量门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("数据角色/质量门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
