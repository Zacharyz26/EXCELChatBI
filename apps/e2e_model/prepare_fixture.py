"""Create the deterministic Excel input consumed by Compose browser E2E."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def build_sales_fixture() -> pd.DataFrame:
    """构造可同时覆盖画像与真实趋势分析的确定性销售数据。"""
    return pd.DataFrame(
        {
            "月份": [
                "2026-01",
                "2026-02",
                "2026-03",
                "2026-04",
                "2026-05",
                "2026-06",
            ],
            "地区": ["华东", "华南", "华东", "华南", "华东", "华南"],
            "销售额": [120, 95, 140, 130, 155, 170],
        }
    )


def main() -> None:
    output_dir = Path(os.getenv("E2E_FIXTURE_DIR", "/fixtures"))
    output_dir.mkdir(parents=True, exist_ok=True)
    build_sales_fixture().to_excel(output_dir / "sales.xlsx", index=False)


if __name__ == "__main__":
    main()
