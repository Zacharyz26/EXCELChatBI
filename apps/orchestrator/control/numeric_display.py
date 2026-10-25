"""Shared numeric lexemes and display scales; never normalize whole identifiers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from apps.orchestrator.control.statistic_statements import (
    NumericValueOwner,
    lexical_quantity_word_spans,
    numeric_value_owner,
    paired_quote_spans,
)

# Only decimal digits and the explicitly supported numeric punctuation. Do not
# NFKC text: compatibility numerals, field names and units retain their spelling.
NUMBER_BODY = r"[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
PERCENT_SIGNS = "%％"
NUMBER_LITERAL = re.compile(NUMBER_BODY + r"[%％]?")
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.\-])" + NUMBER_LITERAL.pattern + r"(?![A-Za-z0-9_.\-])"
)
_PERCENT_SUFFIX = re.compile(r"\s*[%％]")
_LARGE_UNIT = re.compile(r"\s*([千万亿])")
_FACTORS = {"千": Decimal(1000), "万": Decimal(10000), "亿": Decimal(100000000)}
# This is an allowed terminal grammar, not a list of forbidden unit symbols.
# An opening bracket, currency/physical unit, extra percentage or any other
# attachment cannot be consumed as an end of a ratio measurement.
_RATIO_END = re.compile(r"\s*(?:[)）\]】」』”’]\s*)*[。.!！?？;；]?\s*")


def decimal_from_token(token: str) -> Decimal | None:
    if NUMBER_LITERAL.fullmatch(token) is None:
        return None
    numeric = token[:-1] if token.endswith(tuple(PERCENT_SIGNS)) else token
    try:
        value = Decimal(numeric.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def display_quantum(token: str) -> Decimal | None:
    """Return the decimal quantum explicitly rendered by one numeric token."""
    normalized = decimal_from_token(token)
    if normalized is None:
        return None
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return Decimal(1).scaleb(exponent)


def matches_display_value(
    evidence_value: Decimal,
    claim_value: Decimal,
    *,
    display_factor: Decimal,
    quantum: Decimal,
) -> bool:
    """Apply the shared deterministic display-rounding rule to one value."""
    try:
        displayed = evidence_value / display_factor
        return displayed.quantize(quantum, rounding=ROUND_HALF_UP) == claim_value
    except (InvalidOperation, ZeroDivisionError):
        return False


@dataclass(frozen=True)
class NumericDisplay:
    """One number's scale and its unconsumed suffix in the original text."""

    percent: bool
    factor: Decimal | None
    remainder: str

    @property
    def ratio_complete(self) -> bool:
        return (
            self.factor in {Decimal(1), Decimal("0.01")}
            and _RATIO_END.fullmatch(self.remainder) is not None
        )


def parse_numeric_display(token: str, suffix: str) -> NumericDisplay:
    """Interpret ASCII/fullwidth percent identically without changing offsets.

    A single percent can be adjacent to the number or separated by whitespace.
    A second sign or a combination with a large-number scale is ambiguous.
    Preserve every unconsumed character so callers can enforce typed units.
    """
    if decimal_from_token(token) is None:
        return NumericDisplay(False, None, suffix)
    percent = token.endswith(tuple(PERCENT_SIGNS))
    remainder = suffix
    mark = _PERCENT_SUFFIX.match(remainder)
    if not percent and mark is not None:
        percent = True
        remainder = remainder[mark.end() :]
    if _PERCENT_SUFFIX.match(remainder) is not None:
        return NumericDisplay(percent, None, remainder)
    large = _LARGE_UNIT.match(remainder)
    if percent:
        return NumericDisplay(True, None if large else Decimal("0.01"), remainder)
    if large:
        if _PERCENT_SUFFIX.match(remainder[large.end() :]) is not None:
            return NumericDisplay(False, None, remainder)
        return NumericDisplay(False, _FACTORS[large[1]], remainder)
    return NumericDisplay(False, Decimal(1), remainder)


def has_numeric_symbol(text: str) -> bool:
    """Use Unicode numeric properties, never implicit compatibility conversion."""
    return any(char.isnumeric() for char in text)


def measurement_value_context(
    text: str, left: int, right: int, measurement_labels: set[str], *, assignment_only: bool = False
) -> bool:
    """数值提取和软换行共享同一个有界所有权判定。"""
    return (
        numeric_value_owner(text, left, right, measurement_labels, assignment_only=assignment_only)
        is not None
    )


@dataclass(frozen=True)
class NumericTextContext:
    """词法三态独立于所有权；可解析不等于有 Evidence 支持。"""

    classification: Literal["ordinary_text", "parsed_numeric", "unsupported_or_ambiguous_numeric"]
    owner: NumericValueOwner | None
    value_span: tuple[int, int]


def numeric_text_context(
    text: str, left: int, right: int, measurement_labels: set[str]
) -> NumericTextContext:
    """保留明确数值义务；未知写法不因后缀词法失败而降为普通文本。"""
    token = text[left:right]
    owner = numeric_value_owner(text, left, right, measurement_labels)
    if not has_numeric_symbol(token):
        # A generic noun alone is not a statistic, but its explicit assignment
        # still creates an obligation for unknown alphabetic values.
        owner = numeric_value_owner(text, left, right, measurement_labels - {"数值"}) or (
            numeric_value_owner(text, left, right, measurement_labels, assignment_only=True)
        )
    if owner is not None:
        return NumericTextContext(
            "parsed_numeric"
            if decimal_from_token(token) is not None
            else "unsupported_or_ambiguous_numeric",
            owner,
            (left, right),
        )
    # Unowned Chinese numeric ideographs occur inside ordinary words. Only an
    # established owner promotes those letters to an unsupported measurement.
    if not any(unicodedata.category(char).startswith("N") for char in token):
        return NumericTextContext("ordinary_text", None, (left, right))
    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.\-]*", token):
        return NumericTextContext("ordinary_text", None, (left, right))
    if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", token):
        try:
            date.fromisoformat(token + "-01" if len(token) == 7 else token)
        except ValueError:
            pass
        else:
            return NumericTextContext("ordinary_text", None, (left, right))
    return NumericTextContext(
        "parsed_numeric"
        if decimal_from_token(token) is not None
        else ("unsupported_or_ambiguous_numeric"),
        None,
        (left, right),
    )


def unrecognized_numeric_spans(
    text: str, covered: list[tuple[int, int]], *, measurement_labels: set[str]
) -> list[tuple[int, int]]:
    """Account for numeric glyphs omitted by the supported decimal grammar.

    Keep original spelling and offsets; do not infer a value from compatibility
    numbers. Lexical identifiers and valid ISO calendar labels remain labels,
    except inside a measurement's value position, including delimiters around
    the value. Delimiters affect context only: never remove enclosed content.
    Known source fields/dimensions are protected by caller-supplied spans.
    """
    result: list[tuple[int, int]] = []
    # Numeric ideographs consumed as existing decimal scales are not new values.
    covered = list(covered)
    for number in NUMBER_PATTERN.finditer(text):
        unit = _LARGE_UNIT.match(text[number.end() :])
        if unit is not None:
            covered.append((number.end(), number.end() + unit.end()))

    def part(char: str) -> bool:
        return char.isnumeric() or char in ".+-_%％" or (char.isascii() and char.isalpha())

    visited_until = 0
    for index, char in enumerate(text):
        if index < visited_until or not (
            char.isnumeric() or (char.isascii() and (char.isalpha() or char == "_"))
        ):
            continue
        if any(a <= index < b for a, b in [*covered, *result]):
            continue
        left, right = index, index + 1
        while left and part(text[left - 1]):
            left -= 1
        while right < len(text) and part(text[right]):
            right += 1
        token = text[left:right].rstrip(".")
        right = left + len(token)
        visited_until = right
        context = numeric_text_context(text, left, right, measurement_labels)
        if context.classification == "ordinary_text":
            continue
        result.append((left, right))
    # Some genuine quantity words (e.g. 半) have no Unicode numeric property.
    # Lexical enumeration only supplies candidates: a bounded owner is still
    # mandatory, and their spelling remains unsupported rather than converted.
    for left, right in lexical_quantity_word_spans(text):
        if any(left < end and start < right for start, end in [*covered, *result]):
            continue
        context = numeric_text_context(text, left, right, measurement_labels)
        if context.owner is not None and context.classification != "ordinary_text":
            result.append((left, right))
    result.sort()
    return result


def numeric_quote_view(
    text: str, measurement_labels: set[str]
) -> tuple[str, list[tuple[int, int]]]:
    """投影成对数值包装，保留原位置；标签引用不产生虚构的赋值。

    这里只识别包装与引用结构，不接受任何数值。字段、角色、范围、
    单位与 Evidence 仍由调用方验证，未闭合包装保持原样并拒绝绑定。
    """
    closed = paired_quote_spans(text)
    view = list(text)
    labels: list[tuple[int, int]] = []
    for opening, left, right, end in closed:
        match = re.fullmatch(r"\s*(" + NUMBER_LITERAL.pattern + r")\s*", "".join(view[left:right]))
        if match is None:
            if text[left:right] in measurement_labels and re.match(
                r"\s*(?:约|大约)?\s*(?:等于|为|是(?!否)|[:：=])", text[end:]
            ):
                view[opening:left] = " " * (left - opening)
                view[right:end] = " " * (end - right)
            continue
        number_span = (left + match.start(1), left + match.end(1))
        owner = numeric_value_owner(text, *number_span, measurement_labels)
        # Adjacent closed quotations are separate labels. An assignment outside
        # the first quotation cannot enter this branch (the gap is not empty).
        sibling = next(
            (
                (a, b, c, d)
                for a, b, c, d in closed
                if d <= opening
                and not text[d:opening].strip()
                and text[b:c].rstrip("：: ") in measurement_labels
            ),
            None,
        )
        if owner is None and sibling is not None:
            labels.append(number_span)
            continue
        view[opening:left] = " " * (left - opening)
        view[right:end] = " " * (end - right)
    return "".join(view), labels
