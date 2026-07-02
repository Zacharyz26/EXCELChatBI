"""策略驱动的采样脱敏（第1层：模型只看脱敏后的画像）。

按"列类型默认行为 + 数据集策略显式覆盖"两级决定每列的采样动作，并同时作用于
`sample_values`（每列样本值）与 `sample_rows`（样本明细行的对应单元格），
堵住"列级采样"与"样本行"两条泄露路径。脱敏实现可插拔（`Redactor`）。
"""

from __future__ import annotations

import abc
from enum import Enum
from typing import TYPE_CHECKING

from packages.governance.data_boundary import (
    ColumnRule,
    EffectivePolicy,
    SensitivityLevel,
)

if TYPE_CHECKING:  # 仅类型标注，避免 governance 运行时耦合具体 MCP 服务
    from mcp_servers.excel_parser.profile import ColumnProfile, DataProfile


class SampleAction(str, Enum):
    """单列的采样动作。"""

    VALUES = "values"        # 给样本值 + 统计
    STATS_ONLY = "stats"     # 不给样本值，仅统计（明细单元格也遮蔽）
    MASK = "mask"            # 样本值打码，保留统计
    EXCLUDE = "exclude"      # 不给样本值也不给统计，仅留 schema


class Redactor(abc.ABC):
    """脱敏器接口（可替换：打码 / 假名化 / 格式保留等）。"""

    @abc.abstractmethod
    def mask(self, value: object) -> str:
        """把单个单元格值替换为脱敏表示。"""


class DefaultRedactor(Redactor):
    """默认脱敏器：统一替换为掩码标记。"""

    def __init__(self, mask_token: str = "***") -> None:
        self._token = mask_token

    def mask(self, value: object) -> str:
        return self._token


_NUMERIC = {"int", "float"}


def resolve_action(col: ColumnProfile, policy: EffectivePolicy) -> SampleAction:
    """依据列类型默认行为 + 显式列规则，决定该列的采样动作。"""
    rule = policy.rule_of(col.name)
    if rule is ColumnRule.EXCLUDE:
        return SampleAction.EXCLUDE
    if rule is ColumnRule.MASK:
        return SampleAction.MASK

    # NORMAL：按类型 + 级别的默认行为
    level = policy.level
    if col.dtype in _NUMERIC or col.dtype == "bool":
        return SampleAction.VALUES
    if col.dtype == "datetime":
        if level is SensitivityLevel.RESTRICTED:
            return SampleAction.STATS_ONLY
        return SampleAction.VALUES

    # 文本列
    if level is SensitivityLevel.RESTRICTED:
        return SampleAction.MASK
    if col.distinct_count <= policy.cutoff_for_level():
        return SampleAction.VALUES
    return SampleAction.STATS_ONLY


def apply_policy(
    profile: DataProfile,
    policy: EffectivePolicy,
    redactor: Redactor | None = None,
) -> DataProfile:
    """就地脱敏画像：处理每列 sample_values / 统计，以及 sample_rows 单元格。

    只有判定为 VALUES 的列，其真实单元格内容才会出现在发给模型的 payload 中。
    """
    red = redactor or DefaultRedactor(policy.mask_token)
    actions: dict[str, SampleAction] = {}

    for col in profile.columns:
        action = resolve_action(col, policy)
        actions[col.name] = action
        if action is SampleAction.VALUES:
            continue
        if action is SampleAction.STATS_ONLY:
            col.sample_values = []
        elif action is SampleAction.MASK:
            col.sample_values = [red.mask(v) for v in col.sample_values]
        elif action is SampleAction.EXCLUDE:
            col.sample_values = []
            col.min = col.max = col.mean = col.std = col.median = None

    for row in profile.sample_rows:
        for name, action in actions.items():
            if action is SampleAction.VALUES or name not in row:
                continue
            row[name] = None if action is SampleAction.EXCLUDE else red.mask(row[name])
    return profile
