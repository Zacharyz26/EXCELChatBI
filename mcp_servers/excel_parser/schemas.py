"""Excel 解析工具的入参 JSON Schema（红线3 校验用）。"""

from __future__ import annotations

from typing import Any

PARSE_EXCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_ref": {"type": "string", "description": "MinIO 中的 Excel 文件引用"},
        "sheet": {"type": "string", "description": "工作表名，可选"},
    },
    "required": ["file_ref"],
    "additionalProperties": False,
}

INFER_SCHEMA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_ref": {"type": "string"},
    },
    "required": ["dataset_ref"],
    "additionalProperties": False,
}

DATA_PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_ref": {"type": "string"},
        "rows": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    "required": ["dataset_ref"],
    "additionalProperties": False,
}
