"""Excel 解析工具的入参 JSON Schema（红线3 校验用）。"""

from __future__ import annotations

from typing import Any

PARSE_EXCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_ref": {"type": "string", "description": "上传文件落盘后的引用路径"},
        "sheet": {"type": "string", "description": "工作表名，可选（默认第一个）"},
        "header_row": {
            "type": "integer",
            "minimum": 0,
            "description": "表头所在行号（0 基），默认 0",
        },
        "nrows": {
            "type": "integer",
            "minimum": 1,
            "description": "最多读取行数，可选（不填读全部）",
        },
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
