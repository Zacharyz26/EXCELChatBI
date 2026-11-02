"""Frozen source-action ownership regression; no model/network or database writes."""

from __future__ import annotations

import pytest
from packages.orchestration.context_selection import source_intent

REF = "a" * 32


@pytest.mark.parametrize(
    "text",
    [
        "筛选组别=A。\n这里只说明表达式“值>1”的含义，暂不应用它。",
        "这里只说明表达式“值>1”的含义，暂不应用它。",
    ],
)
def test_application_is_not_a_source_action(text):
    intent = source_intent(text)
    assert intent.reason_code is None
    assert intent.selection is None
    assert not intent.clauses


@pytest.mark.parametrize(
    "text,only",
    [
        (f"继续使用 dataset_ref={REF}", True),
        (f"请使用 dataset_ref={REF} 查看数据画像和销售额趋势，然后生成报告", False),
    ],
)
def test_complete_explicit_source_keeps_business_obligations(text, only):
    intent = source_intent(text)
    assert intent.reason_code is None
    assert intent.selection is not None and intent.selection.dataset_ref == REF
    assert intent.selection.selection_only is only
    # Complete source identity stays an exact original-source resource.
    start, end = intent.selection.source_span
    assert REF in text[start:end]
    if not only:
        assert "查看数据画像" in text[end:]


@pytest.mark.parametrize(
    "text",
    [
        f"继续使用 dataset_ref={REF}；先别用它。",
        f"不要使用 dataset_ref={REF}。",
        f"不确定要使用 dataset_ref={REF}。",
    ],
)
def test_negative_unknown_or_withdrawn_source_remains_blocked(text):
    intent = source_intent(text)
    assert intent.reason_code is not None
    assert intent.selection is None or intent.selection.reason_code is not None


def test_quoted_source_example_does_not_become_authorization():
    text = f"示例：“继续使用 dataset_ref={REF}”；这里只说明表达式，不应用它。"
    intent = source_intent(text)
    assert intent.reason_code is None
    assert intent.selection is None
    assert intent.ignored_reference_spans
