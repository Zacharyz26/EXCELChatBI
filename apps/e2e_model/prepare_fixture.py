"""Create the deterministic Excel input consumed by Compose browser E2E."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def main() -> None:
    output_dir = Path(os.getenv("E2E_FIXTURE_DIR", "/fixtures"))
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "月份": ["2026-01", "2026-02", "2026-03"],
            "地区": ["华东", "华南", "华东"],
            "销售额": [120, 95, 140],
        }
    ).to_excel(output_dir / "sales.xlsx", index=False)


if __name__ == "__main__":
    main()
