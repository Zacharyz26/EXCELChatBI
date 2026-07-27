"""Prepare an isolated, deterministic workspace for the real full-stack E2E."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
E2E_ROOT = (ROOT / ".data" / "e2e").resolve()


def main() -> None:
    expected_parent = (ROOT / ".data").resolve()
    if E2E_ROOT.parent != expected_parent or E2E_ROOT.name != "e2e":
        raise RuntimeError("拒绝清理非预期的 E2E 数据目录")
    shutil.rmtree(E2E_ROOT, ignore_errors=True)
    E2E_ROOT.mkdir(parents=True)
    pd.DataFrame(
        {
            "月份": ["2026-01", "2026-02", "2026-03"],
            "地区": ["华东", "华南", "华东"],
            "销售额": [120, 95, 140],
        }
    ).to_excel(E2E_ROOT / "sales.xlsx", index=False)


if __name__ == "__main__":
    main()
