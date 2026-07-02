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
    # 数值列统计摘要（describe，非数值列为空）
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    sample_values: list[str] = field(default_factory=list)  # 脱敏样本值

    def to_dict(self) -> dict:
        """转可 JSON 序列化字典（用于喂给 LLM 的 payload）。"""
        return {
            "name": self.name,
            "dtype": self.dtype,
            "null_ratio": round(self.null_ratio, 4),
            "distinct_count": self.distinct_count,
            "min": self.min,
            "max": self.max,
            "mean": self.mean,
            "std": self.std,
            "median": self.median,
            "sample_values": self.sample_values,
        }


@dataclass
class DataProfile:
    """整表数据画像，喂给 LLM 的唯一数据视图（红线1）。

    LLM 绝不接收原始整表，只接收本对象；原始数据由 dataset_ref 在服务端引用。
    """

    dataset_ref: str                # 指向落盘数据集，LLM 不直接读
    row_count: int
    column_count: int
    columns: list[ColumnProfile]
    sample_rows: list[dict] = field(default_factory=list)   # 少量样本行（默认前5，可脱敏）

    def to_dict(self) -> dict:
        """转可 JSON 序列化字典（喂给 LLM 的唯一数据视图）。"""
        return {
            "dataset_ref": self.dataset_ref,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "columns": [c.to_dict() for c in self.columns],
            "sample_rows": self.sample_rows,
        }
