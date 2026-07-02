"""数据边界（三层）测试：策略脱敏 + 小分组保护。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.excel_parser.tools import infer_schema, parse_excel  # noqa: E402
from packages.common.dataset_store import save_metadata  # noqa: E402
from packages.governance.aggregation_guard import GroupAgg, guard_small_groups  # noqa: E402


@pytest.fixture
def secret_ref(tmp_path: Path) -> str:
    df = pd.DataFrame(
        {
            "区域": ["华东"] * 6 + ["华北"] * 6,
            "销售额": [100] * 12,
            "密级备注": ["普通"] * 11 + ["机密内容_XYZ"],  # 低基数，默认会给值
        }
    )
    path = tmp_path / "s.xlsx"
    df.to_excel(path, index=False)
    return parse_excel({"file_ref": str(path)})["dataset_ref"]


# ── 第1层：策略脱敏 ──

def test_mask_and_exclude_not_in_payload(secret_ref: str) -> None:
    save_metadata(
        secret_ref,
        {"policy": {"columns": {"密级备注": "exclude", "区域": "mask"}}},
    )
    profile = infer_schema({"dataset_ref": secret_ref})
    payload = json.dumps(profile.to_dict(), ensure_ascii=False)
    by_name = {c.name: c for c in profile.columns}

    # exclude：无样本值、无统计摘要，且机密内容不在 payload
    assert by_name["密级备注"].sample_values == []
    assert by_name["密级备注"].min is None
    assert "机密内容_XYZ" not in payload

    # mask：样本值被打码；样本行对应单元格也被打码（非真实值）
    assert all(v == "***" for v in by_name["区域"].sample_values)
    assert all(row["区域"] == "***" for row in profile.sample_rows)


def test_default_open_gives_low_card_values(secret_ref: str) -> None:
    # 默认（宽松）下低基数文本给样本值——记录“默认宽松、按需收紧”的取舍。
    profile = infer_schema({"dataset_ref": secret_ref})
    by_name = {c.name: c for c in profile.columns}
    assert "机密内容_XYZ" in by_name["密级备注"].sample_values


def test_restricted_masks_text_keeps_numeric(secret_ref: str) -> None:
    save_metadata(secret_ref, {"policy": {"level": "restricted"}})
    profile = infer_schema({"dataset_ref": secret_ref})
    by_name = {c.name: c for c in profile.columns}
    # 文本列打码，数值列仍给值
    assert all(v == "***" for v in by_name["密级备注"].sample_values)
    assert by_name["销售额"].sample_values  # 数值列保留


# ── 第3层：小分组保护 ──

def test_small_groups_merged_into_other() -> None:
    groups = [
        GroupAgg("A", 100.0, 6),
        GroupAgg("B", 10.0, 2),
        GroupAgg("C", 20.0, 2),
        GroupAgg("D", 30.0, 2),
    ]
    out = guard_small_groups(groups, "sum", 5, mode="merge", other_label="其他")
    by_key = {g.key: g for g in out}
    assert by_key["A"].value == 100.0
    assert "B" not in by_key and "C" not in by_key and "D" not in by_key
    # 合并桶：总量守恒 10+20+30=60，行数 6
    assert by_key["其他"].value == 60.0
    assert by_key["其他"].count == 6


def test_merged_still_too_small_dropped() -> None:
    groups = [GroupAgg("A", 100.0, 6), GroupAgg("B", 5.0, 1)]
    out = guard_small_groups(groups, "sum", 5, mode="merge")
    assert [g.key for g in out] == ["A"]  # 合并后仍<5 → 丢弃


def test_weighted_mean_merge() -> None:
    groups = [GroupAgg("A", 10.0, 6), GroupAgg("B", 2.0, 2), GroupAgg("C", 4.0, 3)]
    out = guard_small_groups(groups, "mean", 5, mode="merge")
    other = next(g for g in out if g.key == "其他")
    # 加权平均 (2*2 + 4*3) / 5 = 3.2
    assert other.value == pytest.approx(3.2)
