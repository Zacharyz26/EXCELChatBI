"""用量完整性边界：保留未知，不从总量或可选分项猜测计费主计数。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeGuard

_MISSING = object()


def is_token_count(value: object) -> TypeGuard[int]:
    """只有明确的非负整数才是可计费计数；布尔值与隐式转换不合法。"""
    return type(value) is int and value >= 0


def sum_token_counts(values: Iterable[object]) -> int | None:
    """任一计数未知或非法时总量仍未知，不能展示部分和作为完整总量。"""
    total = 0
    for value in values:
        if not is_token_count(value):
            return None
        total += value
    return total


@dataclass(frozen=True)
class ParsedUsage:
    """一次完整用量快照；问题原因区分不存在、null、非法和相互矛盾。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    issues: tuple[str, ...] = ("missing_usage",)

    @property
    def available(self) -> bool:
        """仅完整合法的双主计数可用于结算，明确的零值同样合法。"""
        return (
            not self.issues
            and is_token_count(self.prompt_tokens)
            and is_token_count(self.completion_tokens)
        )


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    supplied = getattr(value, "model_fields_set", None)
    if isinstance(supplied, set) and name not in supplied:
        return _MISSING
    return getattr(value, name, _MISSING)


def parse_usage(raw: object) -> ParsedUsage:
    """解析兼容接口的双主计数；可选细分不参与现有双费率的额外收费。"""
    if raw is None:
        return ParsedUsage()
    issues: list[str] = []

    def count(name: str) -> int | None:
        value = _field(raw, name)
        if value is _MISSING:
            issues.append(f"missing_{name}")
        elif value is None:
            issues.append(f"null_{name}")
        elif not is_token_count(value):
            issues.append(f"invalid_{name}")
        else:
            return value
        return None

    prompt = count("prompt_tokens")
    completion = count("completion_tokens")
    total = _field(raw, "total_tokens")
    if total is not _MISSING:
        if not is_token_count(total):
            issues.append("invalid_total_tokens")
        elif prompt is not None and completion is not None and total != prompt + completion:
            issues.append("inconsistent_total_tokens")

    # SDK 契约将这些字段定义为各自主计数的细分，缺失/null均可选；
    # 不累加（细分可能重叠），不推导主计数，也不引入缓存折扣/推理费率。
    for name, parent, fields in (
        ("prompt_tokens_details", prompt, ("cached_tokens", "audio_tokens")),
        (
            "completion_tokens_details",
            completion,
            (
                "reasoning_tokens",
                "audio_tokens",
                "accepted_prediction_tokens",
                "rejected_prediction_tokens",
            ),
        ),
    ):
        details = _field(raw, name)
        if details is _MISSING or details is None:
            continue
        if not isinstance(details, Mapping) and not hasattr(details, "__dict__"):
            issues.append(f"invalid_{name}")
            continue
        for field in fields:
            value = _field(details, field)
            if value is _MISSING or value is None:
                continue
            if not is_token_count(value) or (parent is not None and value > parent):
                issues.append(f"invalid_{name}.{field}")
    return ParsedUsage(prompt, completion, tuple(issues))
