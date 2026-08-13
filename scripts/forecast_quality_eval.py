"""v2.5 6D-4 anonymous governed-forecast quality gate.

The frozen cases describe synthetic generators rather than business rows. The
gate executes the real ``stats.forecast`` implementation, validates its strict
output contract and writes only case-level guard outcomes to the report.
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
from mcp_servers.stats.tools import forecast as _forecast  # noqa: E402
from packages.common.dataset_store import (  # noqa: E402
    delete_dataset,
    save_dataframe,
    save_metadata,
)

DEFAULT_CASES = Path(__file__).parent / "forecast_quality_eval_set.jsonl"
THRESHOLDS: dict[str, float | int] = {
    "expected_outcome_rate": 1.0,
    "output_contract_rate": 1.0,
    "leakage_guard_rate": 1.0,
    "baseline_reliability_rate": 1.0,
    "metric_availability_rate": 1.0,
    "failure_closed_rate": 1.0,
    "bounded_method_violations": 0,
}

_CASE_KEYS = frozenset(
    {"id", "series", "frequency", "request", "fault", "protected", "expected"}
)
_REQUEST_KEYS = frozenset(
    {"horizon", "method", "validation_size", "seasonal_period"}
)
_EXPECTED_KEYS = frozenset(
    {
        "status",
        "selected_method",
        "reliability",
        "beats_baseline",
        "mape_available",
        "error_guard",
    }
)
_METHODS = frozenset({"auto", "naive", "drift", "seasonal_naive"})
_SELECTED_METHODS = frozenset({"naive", "drift", "seasonal_naive"})
_FAULTS = frozenset({"none", "irregular", "duplicate"})
_ERROR_MARKERS = {
    "irregular_frequency": "时间间隔不规则",
    "duplicate_timestamp": "重复时间点",
    "insufficient_sample": "有效时间点不足",
    "validation_shorter_than_horizon": "validation_size 至少覆盖 horizon",
    "insufficient_seasons": "至少需要三个完整周期",
    "protected_column": "受数据策略保护",
}


def forecast_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    """Public seam for executing and contract-drift testing the real tool."""
    return _forecast(arguments)


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
        raise ValueError("预测质量评测用例为空")
    return cases


def _validate_case(case: dict[str, Any], case_id: str) -> None:
    series = case.get("series")
    if not isinstance(series, dict):
        raise ValueError(f"{case_id}: series 契约无效")
    kind = series.get("kind")
    allowed_series_keys = {
        "linear": {"kind", "count", "start", "step"},
        "constant": {"kind", "count", "value"},
        "seasonal": {"kind", "count", "pattern"},
    }
    if kind not in allowed_series_keys or set(series) != allowed_series_keys[kind]:
        raise ValueError(f"{case_id}: series 生成器字段无效")
    count = series.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not 3 <= count <= 365:
        raise ValueError(f"{case_id}: series count 无效")
    numeric_keys = allowed_series_keys[kind] - {"kind", "count", "pattern"}
    if any(
        isinstance(series.get(key), bool)
        or not isinstance(series.get(key), int | float)
        for key in numeric_keys
    ):
        raise ValueError(f"{case_id}: series 数值参数无效")
    pattern = series.get("pattern")
    if kind == "seasonal" and (
        not isinstance(pattern, list)
        or not 2 <= len(pattern) <= 52
        or any(isinstance(item, bool) or not isinstance(item, int | float) for item in pattern)
    ):
        raise ValueError(f"{case_id}: seasonal pattern 无效")
    frequency = case.get("frequency")
    if frequency not in {"D", "W", "MS"}:
        raise ValueError(f"{case_id}: frequency 无效")
    request = case.get("request")
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        raise ValueError(f"{case_id}: request 契约无效")
    if request.get("method") not in _METHODS:
        raise ValueError(f"{case_id}: method 无效")
    for key in ("horizon", "validation_size"):
        value = request.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{case_id}: {key} 无效")
    period = request.get("seasonal_period")
    if period is not None and (
        isinstance(period, bool) or not isinstance(period, int) or period < 2
    ):
        raise ValueError(f"{case_id}: seasonal_period 无效")
    if case.get("fault") not in _FAULTS or not isinstance(case.get("protected"), bool):
        raise ValueError(f"{case_id}: fault/protected 无效")
    expected = case.get("expected")
    if not isinstance(expected, dict) or set(expected) != _EXPECTED_KEYS:
        raise ValueError(f"{case_id}: expected 契约无效")
    status = expected.get("status")
    if status not in {"success", "error"}:
        raise ValueError(f"{case_id}: expected status 无效")
    if status == "success":
        if (
            expected.get("selected_method") not in _SELECTED_METHODS
            or expected.get("reliability") not in {"moderate", "limited"}
            or not isinstance(expected.get("beats_baseline"), bool)
            or not isinstance(expected.get("mape_available"), bool)
            or expected.get("error_guard") is not None
        ):
            raise ValueError(f"{case_id}: success expectation 无效")
    elif (
        any(
            expected.get(key) is not None
            for key in (
                "selected_method",
                "reliability",
                "beats_baseline",
                "mape_available",
            )
        )
        or expected.get("error_guard") not in _ERROR_MARKERS
    ):
        raise ValueError(f"{case_id}: error expectation 无效")


def run_evaluation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Run real deterministic forecasts and emit a data-minimized quality report."""
    rows = [_evaluate_case(case) for case in cases]
    success_rows = [row for row in rows if row["expected_status"] == "success"]
    error_rows = [row for row in rows if row["expected_status"] == "error"]

    def rate(items: list[dict[str, Any]], key: str) -> float:
        return sum(bool(item[key]) for item in items) / len(items) if items else 1.0

    metrics: dict[str, float | int] = {
        "expected_outcome_rate": rate(rows, "outcome_matches"),
        "output_contract_rate": rate(success_rows, "output_contract"),
        "leakage_guard_rate": rate(success_rows, "leakage_guard"),
        "baseline_reliability_rate": rate(success_rows, "baseline_reliability"),
        "metric_availability_rate": rate(success_rows, "metric_availability"),
        "failure_closed_rate": rate(error_rows, "failure_closed"),
        "bounded_method_violations": sum(
            int(row["bounded_method_violation"]) for row in rows
        ),
    }
    misses = {
        name: {"actual": metrics[name], "required": required}
        for name, required in THRESHOLDS.items()
        if metrics[name] != required
    }
    return {
        "evaluation": "v2.5_governed_forecast",
        "case_set_sha256": _stable_hash(cases),
        "case_count": len(rows),
        "success_case_count": len(success_rows),
        "failure_case_count": len(error_rows),
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses and all(row["passed"] for row in rows),
        "misses": misses,
        "contains_series_values": False,
        "contains_column_names": False,
        "contains_dataset_refs": False,
        "contains_predictions": False,
        "reads_user_raw_rows": False,
        "synthetic_series_only": True,
        "tool_calls": len(rows),
        "model_calls": 0,
    }


def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    request = cast(dict[str, Any], case["request"])
    expected = cast(dict[str, Any], case["expected"])
    frame = _frame_for(case)
    dataset_ref = save_dataframe(frame)
    if case["protected"]:
        save_metadata(dataset_ref, {"policy": {"columns": {"signal_value": "exclude"}}})
    result: dict[str, Any] | None = None
    error_text: str | None = None
    try:
        arguments = {
            "dataset_ref": dataset_ref,
            "time_col": "event_time",
            "value_col": "signal_value",
            "horizon": request["horizon"],
            "method": request["method"],
            "validation_size": request["validation_size"],
        }
        if request["seasonal_period"] is not None:
            arguments["seasonal_period"] = request["seasonal_period"]
        result = forecast_tool(arguments)
    except ValueError as exc:
        error_text = str(exc)
    finally:
        delete_dataset(dataset_ref)

    expected_status = str(expected["status"])
    actual_status = "success" if result is not None else "error"
    output_contract = False
    leakage_guard = False
    baseline_reliability = False
    bounded_method_violation = False
    selected_method: str | None = None
    reliability: str | None = None
    beats_baseline: bool | None = None
    mape_available: bool | None = None
    if result is not None:
        try:
            validate_json(
                result,
                tool_output_schema("forecast"),
                code="invalid_tool_output",
                label="预测质量门禁输出",
            )
            output_contract = True
        except MCPProtocolError:
            output_contract = False
        selected_method = cast(str | None, result.get("selected_method"))
        reliability = cast(str | None, result.get("reliability"))
        baseline = result.get("baseline")
        if isinstance(baseline, dict) and isinstance(baseline.get("beats_baseline"), bool):
            beats_baseline = bool(baseline["beats_baseline"])
        validation_metrics = result.get("validation_metrics")
        if isinstance(validation_metrics, dict):
            mape_available = validation_metrics.get("mape") is not None
        leakage = result.get("leakage_checks")
        leakage_guard = isinstance(leakage, dict) and leakage == {
            "passed": True,
            "chronological_split": True,
            "duplicate_timestamps": False,
            "regular_frequency": True,
            "future_target_rows_used": False,
            "preprocessing_fit_on_training_only": True,
        }
        baseline_reliability = (
            beats_baseline is True and reliability == "moderate"
        ) or (beats_baseline is False and reliability == "limited")
        bounded_method_violation = selected_method not in _SELECTED_METHODS

    error_guard = expected.get("error_guard")
    marker = _ERROR_MARKERS.get(str(error_guard)) if error_guard is not None else None
    failure_closed = (
        expected_status != "error"
        or (
            result is None
            and error_text is not None
            and marker is not None
            and marker in error_text
        )
    )
    outcome_matches = (
        actual_status == expected_status
        and (
            expected_status == "error"
            or (
                selected_method == expected["selected_method"]
                and reliability == expected["reliability"]
                and beats_baseline == expected["beats_baseline"]
                and mape_available == expected["mape_available"]
            )
        )
    )
    checks = {
        "outcome_matches": outcome_matches,
        "output_contract": output_contract if expected_status == "success" else True,
        "leakage_guard": leakage_guard if expected_status == "success" else True,
        "baseline_reliability": (
            baseline_reliability if expected_status == "success" else True
        ),
        "metric_availability": (
            mape_available == expected["mape_available"]
            if expected_status == "success"
            else True
        ),
        "failure_closed": failure_closed,
        "bounded_method_violation": bounded_method_violation,
    }
    return {
        "id": case_id,
        "expected_status": expected_status,
        "actual_status": actual_status,
        "selected_method": selected_method,
        "reliability": reliability,
        "beats_baseline": beats_baseline,
        "mape_available": mape_available,
        **checks,
        "passed": (
            all(value for key, value in checks.items() if key != "bounded_method_violation")
            and not bounded_method_violation
        ),
    }


def _frame_for(case: dict[str, Any]) -> pd.DataFrame:
    series = cast(dict[str, Any], case["series"])
    count = int(series["count"])
    if series["kind"] == "linear":
        values = [float(series["start"]) + float(series["step"]) * index for index in range(count)]
    elif series["kind"] == "constant":
        values = [float(series["value"])] * count
    else:
        pattern = cast(list[float | int], series["pattern"])
        values = [float(pattern[index % len(pattern)]) for index in range(count)]
    times = list(pd.date_range("2024-01-01", periods=count, freq=str(case["frequency"])))
    if case["fault"] == "irregular":
        times[-1] = times[-1] + pd.Timedelta(days=2)
    elif case["fault"] == "duplicate":
        times[-1] = times[-2]
    return pd.DataFrame({"event_time": times, "signal_value": values})


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _print_human(report: dict[str, Any]) -> None:
    print(f"受治理预测：{report['case_count']} 个匿名合成场景")
    metrics = cast(dict[str, float | int], report["metrics"])
    for name in THRESHOLDS:
        value = metrics[name]
        print(f"- {name}: {value:.1%}" if isinstance(value, float) else f"- {name}: {value}")
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 6D-4 受治理预测质量门禁")
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
        print(f"受治理预测用例契约有效：{len(cases)} cases")
        return 0
    report = run_evaluation(cases)
    _print_human(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 报告：{output}")
    if args.enforce and not report["passed"]:
        print(f"受治理预测门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("受治理预测门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
