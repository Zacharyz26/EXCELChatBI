"""Production statistics -> Artifact -> Markdown/PDF value-preservation contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import weasyprint
from apps.orchestrator.artifacts import artifact_payload_for
from mcp_servers.report.server import build_server as build_report_server
from mcp_servers.stats.server import build_server as build_stats_server
from packages.common.analysis_kinds import REPORT_ANALYSIS_KINDS
from packages.common.dataset_store import save_dataframe


@pytest.fixture
def computed_ref() -> str:
    count = 36
    x = np.arange(count, dtype=float)
    rng = np.random.default_rng(73)
    anomaly = 100 + rng.normal(0, 1, count)
    anomaly[10] = 500.0
    x2 = rng.normal(0, 1, count)
    groups = np.repeat(["A", "B", "C"], 12)
    comparison = np.concatenate(
        [rng.normal(2, 0.5, 12), rng.normal(6, 0.5, 12), rng.normal(10, 0.5, 12)]
    )
    return save_dataframe(
        pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=count, freq="D"),
                "trend_value": 10 + 0.5 * x + 3 * np.sin(2 * np.pi * x / 12),
                "forecast_value": x + 1,
                "anomaly_value": anomaly,
                "x1": x,
                "x2": x2,
                "target": 5 + 2.345 * x + 3.21 * x2,
                "group": groups,
                "comparison_value": comparison,
                "sales": x + 10,
            }
        )
    )


def _key_value(tool: str, result: dict[str, Any]) -> object:
    if tool == "trend_analysis":
        return result["slope"]
    if tool == "forecast":
        return result["predictions"][0]["point"]
    if tool == "anomaly_detect":
        return result["anomalies"][0]["value"]
    if tool == "regression":
        return next(item["coef"] for item in result["coefficients"] if item["name"] == "x1")
    if tool == "correlation":
        return result["matrix"][0][1]
    if tool == "dimension_contribution":
        return result["groups"][0]["value"]
    if tool == "group_compare":
        return result["groups"][0]["mean"]
    raise AssertionError(f"unhandled production tool: {tool}")


def _display(value: object) -> str:
    return f"{value:.4g}" if isinstance(value, float) else str(value)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "trend_analysis",
            {
                "value_col": "trend_value",
                "time_col": "date",
                "method": "stl",
                "period": 12,
                "forecast_horizon": 3,
            },
        ),
        (
            "forecast",
            {
                "value_col": "forecast_value",
                "time_col": "date",
                "horizon": 3,
                "method": "auto",
                "validation_size": 6,
            },
        ),
        ("anomaly_detect", {"value_col": "anomaly_value", "method": "iqr"}),
        ("regression", {"target": "target", "features": ["x1", "x2"], "kind": "ols"}),
        ("correlation", {"columns": ["x1", "target"], "method": "pearson"}),
        (
            "dimension_contribution",
            {"dimension_col": "group", "value_col": "sales", "method": "sum"},
        ),
        ("group_compare", {"group_col": "group", "value_col": "comparison_value"}),
    ],
)
def test_computed_values_survive_artifact_markdown_and_pdf(
    computed_ref: str,
    tool: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = build_stats_server()._tools[tool].invoke({"dataset_ref": computed_ref, **arguments})
    payload = artifact_payload_for(tool, result)
    expected = _display(_key_value(tool, result))
    report = (
        build_report_server()
        ._tools["gen_report_md"]
        .invoke(
            {
                "title": "computed contract",
                "profile": {"row_count": 36, "columns": []},
                "stats": [payload],
            }
        )
    )

    assert payload["kind"] in REPORT_ANALYSIS_KINDS
    assert expected in report["markdown"]
    captured: dict[str, str] = {}
    real_html = weasyprint.HTML

    class CapturingHTML:
        def __init__(self, *, string: str, url_fetcher: object) -> None:
            captured["html"] = string
            self._document = real_html(string=string, url_fetcher=url_fetcher).render()

        def write_pdf(self) -> bytes:
            rendered: list[str] = []
            for page in self._document.pages:
                for box in page._page_box.descendants():
                    text = getattr(box, "text", None)
                    if isinstance(text, str):
                        rendered.append(text)
            captured["rendered_text"] = "".join(rendered)
            return self._document.write_pdf()

    monkeypatch.setattr(weasyprint, "HTML", CapturingHTML)
    exported = build_report_server()._tools["export_pdf"].invoke({"report_id": report["report_id"]})

    assert expected in captured["html"]
    assert expected in captured["rendered_text"]
    assert Path(exported["pdf_path"]).read_bytes().startswith(b"%PDF-")
