"""数据集变换/聚合工具入参 JSON Schema（红线3）。

枚举白名单即安全边界（决策 3 修订）：所有操作显式列举，**无自由 SQL 入口**；
additionalProperties 一律 False，模型幻造的参数在 Tool.invoke 即被拒绝。
"""

from __future__ import annotations

from typing import Any

# 过滤算子白名单：比较 / 集合 / 文本包含 / 空值判断
_FILTER_OPS = ["==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains", "is_null", "not_null"]

TRANSFORM_DATASET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_ref": {"type": "string", "description": "源数据集引用"},
        "filters": {
            "type": "array",
            "description": "行过滤条件列表（AND 连接）",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "op": {"type": "string", "enum": _FILTER_OPS},
                    # in/not_in 传数组；is_null/not_null 不需要 value
                    "value": {
                        "type": ["number", "string", "boolean", "array"],
                        "items": {"type": ["number", "string", "boolean"]},
                    },
                },
                "required": ["column", "op"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "drop_nulls": {
            "type": "array",
            "description": "去掉这些列为空的行；空数组表示任一列为空即去",
            "items": {"type": "string"},
        },
        "drop_duplicates": {
            "type": "array",
            "description": "按这些列去重（保留首行）；空数组表示整行全同才去",
            "items": {"type": "string"},
        },
        "sort": {
            "type": "array",
            "description": "排序键（依次生效）",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "order": {"type": "string", "enum": ["asc", "desc"]},
                },
                "required": ["column"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "exclude_row_indices": {
            "type": "array",
            "description": "排除的行号（0 起、源数据集行位置；如异常检测结果的 index）",
            "items": {"type": "integer", "minimum": 0},
            "minItems": 1,
        },
    },
    "required": ["dataset_ref"],
    "additionalProperties": False,
}

AGGREGATE_PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_ref": {"type": "string", "description": "数据集引用"},
        "group_col": {"type": "string", "description": "分组维度列"},
        "value_col": {"type": "string", "description": "度量列；agg=count 时可省略"},
        "agg": {"type": "string", "enum": ["sum", "mean", "count"]},
        "sort": {
            "type": "string",
            "enum": ["value_desc", "value_asc", "group"],
            "description": "结果排序，默认 value_desc",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "默认 20"},
    },
    "required": ["dataset_ref", "group_col", "agg"],
    "additionalProperties": False,
}
