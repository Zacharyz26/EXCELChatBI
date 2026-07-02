"""数据边界策略：数据集级安全策略的结构、默认值与解析（红线1）。

三层边界的策略中枢，**场景无关**：不假设任何行业或写死业务列名，敏感度与规则
一律由配置 / 数据集元数据驱动。默认宽松、按需收紧。

- 第1层脱敏由 `redaction` 依据本策略执行；
- 第3层小分组保护由 `aggregation_guard` 依据本策略执行。

策略来源三层，越具体越优先合并：
  内置默认(宽松) ⊕ config/data_policy.yaml 的 default ⊕ 数据集 sidecar 元数据
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from packages.common.config import get_settings
from packages.common.dataset_store import load_metadata


class SensitivityLevel(str, Enum):
    """数据集整体敏感级别（可扩展）。默认 open（最宽松）。"""

    OPEN = "open"            # 宽松：低基数分类/数值给样本值，高基数文本只给统计
    INTERNAL = "internal"   # 收紧：低基数阈值更小，更多文本列不给样本值
    RESTRICTED = "restricted"  # 严格：文本列样本值打码，仅数值列给值


class ColumnRule(str, Enum):
    """列级规则（显式覆盖按类型的默认行为）。"""

    NORMAL = "normal"       # 用按类型的默认行为
    MASK = "mask"           # 样本值打码，保留统计摘要
    EXCLUDE = "exclude"     # 不给样本值也不给统计摘要，仅留 schema


# 各级别的“低基数”判定阈值：文本列 distinct ≤ 阈值 才给样本值。
_DEFAULT_LOW_CARD_CUTOFF: dict[str, int] = {
    SensitivityLevel.OPEN.value: 20,
    SensitivityLevel.INTERNAL.value: 8,
    SensitivityLevel.RESTRICTED.value: 0,
}


@dataclass
class EffectivePolicy:
    """解析合并后的生效策略，供 redaction 与 aggregation_guard 使用。"""

    level: SensitivityLevel = SensitivityLevel.OPEN
    columns: dict[str, ColumnRule] = field(default_factory=dict)
    low_card_cutoff: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_LOW_CARD_CUTOFF)
    )
    small_group_min_size: int = 5
    small_group_mode: str = "merge"   # merge | drop
    other_label: str = "其他"
    mask_token: str = "***"

    def cutoff_for_level(self) -> int:
        """当前级别的低基数阈值。"""
        return self.low_card_cutoff.get(self.level.value, 0)

    def rule_of(self, column: str) -> ColumnRule:
        """某列的显式规则（无则 NORMAL）。"""
        return self.columns.get(column, ColumnRule.NORMAL)


def _builtin_defaults() -> EffectivePolicy:
    """内置默认策略（宽松），在无任何配置时也能安全工作。"""
    return EffectivePolicy()


def load_defaults(path: str | None = None) -> EffectivePolicy:
    """从 config/data_policy.yaml 的 `default` 段加载默认策略；缺失则用内置默认。"""
    policy_path = Path(path or get_settings().data_policy_path)
    if not policy_path.exists():
        return _builtin_defaults()
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    default = raw.get("default") or {}
    base = _builtin_defaults()
    if "level" in default:
        base.level = SensitivityLevel(default["level"])
    if "low_cardinality_max_distinct" in default:
        base.low_card_cutoff = {**base.low_card_cutoff, **default["low_cardinality_max_distinct"]}
    base.small_group_min_size = int(default.get("small_group_min_size", base.small_group_min_size))
    base.small_group_mode = str(default.get("small_group_mode", base.small_group_mode))
    base.other_label = str(default.get("other_label", base.other_label))
    base.mask_token = str(default.get("mask_token", base.mask_token))
    return base


def parse_policy_override(data: dict[str, Any]) -> dict[str, Any]:
    """把外部传入 / sidecar 的策略字典解析为可合并的片段。

    仅接受受支持字段，忽略未知键（面向未来扩展保持宽容）。
    """
    override: dict[str, Any] = {}
    if "level" in data:
        override["level"] = SensitivityLevel(data["level"])
    if "columns" in data and isinstance(data["columns"], dict):
        override["columns"] = {
            str(name): ColumnRule(rule) for name, rule in data["columns"].items()
        }
    if "small_group_min_size" in data:
        override["small_group_min_size"] = int(data["small_group_min_size"])
    if "small_group_mode" in data:
        override["small_group_mode"] = str(data["small_group_mode"])
    return override


def resolve_policy(
    dataset_ref: str | None = None,
    *,
    defaults: EffectivePolicy | None = None,
) -> EffectivePolicy:
    """解析某数据集的生效策略：内置/配置默认 ⊕ 数据集 sidecar 元数据。

    Args:
        dataset_ref: 数据集引用；提供时读取其 sidecar 中的 policy 覆盖。
        defaults: 预加载的默认策略（可选，避免重复读盘）。
    """
    policy = defaults or load_defaults()
    if dataset_ref:
        meta = load_metadata(dataset_ref) or {}
        override_src = meta.get("policy")
        if isinstance(override_src, dict):
            override = parse_policy_override(override_src)
            if "level" in override:
                policy.level = override["level"]
            if "columns" in override:
                policy.columns = {**policy.columns, **override["columns"]}
            if "small_group_min_size" in override:
                policy.small_group_min_size = override["small_group_min_size"]
            if "small_group_mode" in override:
                policy.small_group_mode = override["small_group_mode"]
    return policy
