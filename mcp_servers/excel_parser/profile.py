"""数据画像（红线1：LLM 绝不直接处理 Excel 原始数据）。

分析全程只把"画像"（schema / 统计摘要 / 少量样本行）喂给推理模型，
原始整表不进 LLM。本结构是该约束的落点。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnProfile:
    """单列画像。"""

    name: str
    dtype: str                      # 推断类型：int/float/str/datetime/bool/...
    null_ratio: float               # 空值率
    distinct_count: int             # 不同值数量
    # 数值列统计摘要（非数值列可为空）
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None
    sample_values: list[str] = field(default_factory=list)  # 脱敏样本值


@dataclass
class DataProfile:
    """整表数据画像，喂给 LLM 的唯一数据视图。"""

    dataset_ref: str                # 指向原始数据（MinIO / DuckDB），LLM 不直接读
    row_count: int
    columns: list[ColumnProfile]
    sample_rows: list[dict] = field(default_factory=list)   # 少量样本行（可脱敏）
