"""Pure shared source delimiters; no task or statistical interpretation."""

from __future__ import annotations

import re


def hide_request_text(text: str) -> str:
    """Hide request syntax without shifting a surviving instruction's span."""
    return "".join(char if char in "\r\n" else " " for char in text)


def paired_request_examples(text: str) -> str:
    """Mask only closed explicit request-example blocks, never factual answers.

    This is the existing knowledge-request boundary rule shared with operation
    compilation. An unclosed label cannot discard subsequent instructions.
    """
    lines = text.splitlines(keepends=True)
    start: int | None = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"\s*(?:以下|下面)(?:只是|仅为|是)?(?:示例|例子|引用)[:：]\s*", line):
            if start is None:
                start = i
        elif start is not None and re.fullmatch(
            r"\s*(?:以上|上述)(?:不是本次请求|仅为示例|只是示例)[。.]?\s*", line
        ):
            lines[start : i + 1] = [hide_request_text(value) for value in lines[start : i + 1]]
            start = None
    return "".join(lines)


def quote_token_at(text: str, position: int) -> str | None:
    """识别原位置的引号；转义符和英文词内撇号不改变引号方向。"""
    char = text[position]
    if char not in "\"'`“”‘’「」『』":
        return None
    slash = position
    while slash and text[slash - 1] == "\\":
        slash -= 1
    if (position - slash) % 2:
        return None
    if (
        char == "'"
        and 0 < position < len(text) - 1
        and all(c.isascii() and c.isalnum() for c in (text[position - 1], text[position + 1]))
    ):
        return None
    return char


# Delimiters describe assertion scope, never regions to skip numeric checks.
_PAIRS = dict(zip("(（[［{｛【「『〈《“‘", ")）]］}｝】」』〉》”’", strict=True))


def delimited_spans(text: str) -> list[tuple[int, int]]:
    """Keep nested bracket/quote content together, including an unclosed tail."""
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in "\"'`“”‘’「」『』" and quote_token_at(text, index) is None:
            continue
        if stack and char == stack[-1][0]:
            _, start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
            continue
        if char in _PAIRS:
            stack.append((_PAIRS[char], index))
        elif char in "\"'`":
            stack.append((char, index))
    if stack:
        spans.append((stack[0][1], len(text)))
    return spans
