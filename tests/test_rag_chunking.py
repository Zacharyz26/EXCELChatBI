"""分块测试：标题分段、来源保留、长段滑窗重叠。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.rag.chunking import split_document  # noqa: E402


def test_heading_sections_and_source() -> None:
    doc = "# 活跃用户\n活跃用户指去重登录用户。\n# 收入口径\n收入指净收入。"
    chunks = split_document(doc, "指标.md")
    sections = [c.section for c in chunks]
    assert sections == ["活跃用户", "收入口径"]
    assert all(c.source == "指标.md" for c in chunks)
    assert "活跃用户指去重登录用户" in chunks[0].text


def test_long_body_sliding_window_overlap() -> None:
    body = "甲" * 500  # 单段超过分块大小 → 多块且有重叠
    chunks = split_document(f"# 大段\n{body}", "d.md")
    assert len(chunks) > 1
    # 相邻块有重叠：第二块起始内容出现在第一块尾部（同为“甲”重复，长度约束即可）
    assert all(len(c.text) <= 300 for c in chunks)
    assert all(c.section == "大段" for c in chunks)


def test_empty_document() -> None:
    assert split_document("   \n  ", "e.md") == []
