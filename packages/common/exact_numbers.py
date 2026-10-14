"""JSON/浏览器边界的无损数值：超出 binary64 整数范围时使用显式类型包装。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
EXACT_NUMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["exact_number", "number_type"],
    "properties": {
        "exact_number": {"type": "string", "pattern": r"^-?\d+(?:\.\d+)?$"},
        "number_type": {"enum": ["integer", "decimal"]},
    },
}


def exact_number(value: Any) -> Any:
    """保留普通 JSON 数值；大整数/Decimal 明确编码，不借 float 中转。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        return {"exact_number": str(value), "number_type": "integer"}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("非有限 Decimal 无法输出")
        if value == value.to_integral_value():
            return exact_number(int(value))
        return {"exact_number": format(value, "f"), "number_type": "decimal"}
    return value


def decode_exact_number(value: Any) -> Decimal | None:
    """只识别完整的类型包装，普通文本不得伪装成 typed 数值证据。"""
    if not isinstance(value, dict) or set(value) != {"exact_number", "number_type"}:
        return None
    import re

    text = value.get("exact_number")
    if value.get("number_type") not in {"integer", "decimal"} or not isinstance(text, str):
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text) is None:
        return None
    number = Decimal(text)
    if value["number_type"] == "integer" and number != number.to_integral_value():
        return None
    return number
