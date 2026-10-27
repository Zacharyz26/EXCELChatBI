"""筛选/清洗动作的跨表达、跨消费者语义测试。"""

from __future__ import annotations

import pytest
from apps.orchestrator.control.operation_intent import (
    current_operation_requested,
    operation_mentions,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("暂不筛选或清洗", [("筛选", "prohibited"), ("清洗", "prohibited")]),
        ("未做任何筛选或清洗", [("筛选", "absent"), ("清洗", "absent")]),
        ("先画像，以后再清洗", [("清洗", "deferred")]),
        ("只给清洗建议", [("清洗", "described")]),
        ("不要清洗但筛选设备=A", [("清洗", "prohibited"), ("筛选", "current")]),
        ("不筛选，先不清洗", [("筛选", "prohibited"), ("清洗", "prohibited")]),
        ("不要应用清洗规则", [("清洗", "prohibited")]),
        ("无需采用清洗方案", [("清洗", "prohibited")]),
        ("筛选状态不等于停机", [("筛选", "current")]),
        ("按规则清洗数据", [("清洗", "current")]),
        ("执行清洗方案", [("清洗", "current")]),
        ("应用清洗规则", [("清洗", "current")]),
    ],
)
def test_operation_mentions_classify_bounded_language(text, expected):
    assert [(item.action, item.disposition) for item in operation_mentions(text)] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("暂不筛选或清洗", False),
        ("先别清洗", False),
        ("先画像以后清洗", False),
        ("只给清洗建议", False),
        ("检查清洗结果", False),
        ("不筛选，先不清洗", False),
        ("不要应用清洗规则", False),
        ("无需采用清洗方案", False),
        ("不要清洗但筛选设备=A", True),
        ("执行清洗方案", True),
        ("应用清洗规则", True),
        ("筛选状态不等于停机", True),
        ("清洗数据并去掉空值", True),
        ("把‘清洗’作为示例词", False),
    ],
)
def test_current_operation_requested(text, expected):
    assert current_operation_requested(text, actions=("筛选", "过滤", "排除", "清洗")) is expected
