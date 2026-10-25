"""统计赋值先形成值槽；字符是否像数字不能决定陈述是否需要核验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import jieba
import jieba.posseg

ValueOrigin = Literal["statistic_assignment", "direct_statistic", "count", "relative_quantity"]


@dataclass(frozen=True)
class NumericValueOwner:
    """数值义务的局部来源；不表示语法已解析或 Evidence 已支持。"""

    origin: ValueOrigin
    owner_span: tuple[int, int]
    value_span: tuple[int, int]


# Only opening wrappers can introduce a value. A closing/reopening quotation
# separates labels and must not transfer ownership to the next quoted label.
_VALUE_OPEN = r"[\s(（\[［{｛【「『〈《\"'`“‘]*"
_VALUE_CLOSE = r"[\s)）\]］}｝】」』〉》\"'`”’]*"
_COUNT_OBJECT = r"(?:缺失值|空值|不同取值|重复值|重复记录|重复行|记录|重复)"
_COUNT_PREFIX = re.compile(r"(?:共有|总共(?:有)?|另有|存在|有)\s*(?:约|大约)?" + _VALUE_OPEN + r"$")


@lru_cache(maxsize=1)
def _lexical_tokenizer() -> jieba.posseg.POSTokenizer:
    # Own the tokenizer: application-added dictionary entries must not change
    # validation. Only the installed bundled dictionary, never HMM inference.
    return jieba.posseg.POSTokenizer(jieba.Tokenizer())


@lru_cache(maxsize=256)
def _lexical_atoms(text: str) -> tuple[tuple[str, str], ...]:
    return tuple((item.word, item.flag) for item in _lexical_tokenizer().cut(text, HMM=False))


def lexical_quantity_word_spans(text: str) -> list[tuple[int, int]]:
    """补充 Unicode 数值属性未覆盖的数量词；仍须单独证明局部所有权。"""
    spans = []
    offset = 0
    for word, role in _lexical_atoms(text):
        end = offset + len(word)
        if role == "m" and not any(char.isnumeric() for char in word):
            spans.append((offset, end))
        offset = end
    return spans


def _ordinary_numeric_subword(text: str, left: int, right: int) -> bool:
    """只用局部真实词边界识别普通词内的数值字形，不推断未知词。"""
    if not text[left:right] or not all(char.isnumeric() for char in text[left:right]):
        return False
    # A bounded lexical window, ending at existing clause/quote boundaries.
    start = max(0, left - 32)
    end = min(len(text), right + 32)
    boundaries = list(re.finditer(r"[，,。；;!?！？\n\"'`“”‘’「」『』]", text[start:left]))
    if boundaries:
        start += boundaries[-1].end()
    boundary = re.search(r"[，,。；;!?！？\n\"'`“”‘’「」『』]", text[right:end])
    if boundary:
        end = right + boundary.start()
    offset = start
    for word, role in _lexical_atoms(text[start:end]):
        word_end = offset + len(word)
        if offset == left and right < word_end and role != "m":
            return True
        offset = word_end
    return False


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


def _closes_quote_at(text: str, position: int) -> bool:
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'", "`": "`"}
    stack: list[str] = []
    for index in range(position):
        char = quote_token_at(text, index)
        if char is None:
            continue
        if stack and char == stack[-1]:
            stack.pop()
        elif char in pairs:
            stack.append(pairs[char])
    return bool(stack and position < len(text) and quote_token_at(text, position) == stack[-1])


def _crosses_quote_close(text: str, start: int, end: int) -> bool:
    return any(
        _closes_quote_at(text, index)
        for index in range(start, end)
        if quote_token_at(text, index) is not None
    )


def _assignment_pattern(labels: str) -> re.Pattern[str]:
    quoted = "|".join(
        re.escape(a) + r"(?:" + labels + r")" + re.escape(b)
        for a, b in (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'"), ("`", "`"))
    )
    return re.compile(
        r"(?:"
        + quoted
        + "|(?:"
        + labels
        + r")(?P<label_close>[」』”’\"'`])?)\s*(?:约|大约)?\s*(?:等于|为|是(?!否)|[:：=])",
        re.I,
    )


def numeric_value_owner(
    text: str,
    left: int,
    right: int,
    measurement_labels: set[str],
    *,
    assignment_only: bool = False,
) -> NumericValueOwner | None:
    """统一直接统计、赋值及有界计数来源，原文位置不因词法识别而消失。"""
    if not measurement_labels:
        return None
    # A quantity-shaped word can be part of the statistic's own name (such as
    # 多余 in 重复多余记录数). A shorter label must not assign that inner word
    # a value role. Keep this boundary independent of the lexer's word list.
    if any(
        match.start() <= left < right <= match.end() and match.end() - match.start() > right - left
        for label in measurement_labels
        if label
        for match in re.finditer(re.escape(label), text, re.I)
    ):
        return None
    prefix, suffix = text[:left], text[right:]
    ordinary_word = _ordinary_numeric_subword(text, left, right)
    labels = "|".join(
        re.escape(label) for label in sorted(measurement_labels, key=len, reverse=True) if label
    )
    # Assignment slots are also used by the missing-value guard. Preserve its
    # unknown-text obligation, without borrowing a number across a clause.
    for slot in reversed(statistic_value_slots(text, measurement_labels, set())):
        value_start = slot.statement_span[0] + len(slot.label)
        if value_start <= left <= right <= slot.value_span[1]:
            tail = text[value_start:left]
            if (
                not ordinary_word
                and not any(char.isnumeric() for char in tail)
                and not re.search(r"[，,。；;!?！？”’」』]", tail)
                and not _crosses_quote_close(text, value_start, left)
            ):
                return NumericValueOwner("statistic_assignment", slot.statement_span, (left, right))
    if assignment_only:
        return None
    direct = re.search(r"(?:" + labels + r")\s*(?:约|大约)?" + _VALUE_OPEN + r"$", prefix, re.I)
    if (
        direct is not None
        and not ordinary_word
        and not _crosses_quote_close(text, direct.start(), left)
    ):
        return NumericValueOwner("direct_statistic", (direct.start(), right), (left, right))

    # Generic/ordinal determiners are lexical structure, not the count one.
    # Do this locally: an earlier question or disclaimer cannot exempt a later
    # affirmative count. Nor can a predicate turn 参与/参考 into a number.
    if ordinary_word or re.search(r"(?:同|第|任|每|某)$", prefix):
        return None

    relative = re.search(
        r"(?:" + labels + r")\s*(?:增加|减少|增长|下降|提升|降低)(?:了)?\s*" + _VALUE_OPEN + r"$",
        prefix,
        re.I,
    )
    multiplier = re.match(_VALUE_CLOSE + r"倍(?=[。；;，,!?！？\s]|$)", suffix)
    if (
        relative is not None
        and multiplier is not None
        and not _crosses_quote_close(text, relative.start(), left)
    ):
        return NumericValueOwner(
            "relative_quantity", (relative.start(), right + multiplier.end()), (left, right)
        )

    # A quantity WORD must be directly followed by a statistic object. The
    # installed dictionary supplies lexical roles, not a growing counter list.
    # In particular, never search across arbitrary prose for a distant object.
    # This preserves an unsupported obligation; it never converts the word.
    count = _COUNT_PREFIX.search(prefix)
    if count is not None and not _crosses_quote_close(text, count.start(), left):
        closing = re.match(_VALUE_CLOSE, suffix)
        assert closing is not None
        amount_tail = suffix[closing.end() :]
        atoms = _lexical_atoms(text[left:right] + amount_tail[:32])
        atom = next(iter(atoms), None)
        if atom is not None and atom[1] == "m" and atom[0].startswith(text[left:right]):
            counter_end = len(atom[0]) - (right - left)
            noun = re.match(_VALUE_OPEN + _COUNT_OBJECT, amount_tail[counter_end:])
            # A separately tokenized nominal unit is an AMBIGUOUS amount,
            # never a recognized counter or a converted value. It can only be
            # one adjacent lexical atom followed immediately by the object;
            # verbs/connectives cannot bridge to a distant statistic noun.
            if noun is None and len(atoms) > 1 and atoms[1][1] in {"q", "n", "ng", "zg"}:
                counter_end += len(atoms[1][0])
                noun = re.match(_VALUE_OPEN + _COUNT_OBJECT, amount_tail[counter_end:])
            if noun is not None:
                end = right + closing.end() + counter_end + noun.end()
                return NumericValueOwner("count", (count.start(), end), (left, right))

    # Existing bare postfix syntax remains bounded to its established grammar.
    # New/unknown counters need the explicit predicate/object structure above.
    postfix = re.match(
        _VALUE_CLOSE + _VALUE_OPEN + r"(?:个|条|组|行|列)?" + _VALUE_OPEN + _COUNT_OBJECT,
        suffix,
    )
    if postfix is not None:
        before = text[left - 1] if left else ""
        if (
            text[left:right].isspace()
            or not before
            or not before.isalpha()
            or before in "约为是有共计达近超"
        ):
            return NumericValueOwner("count", (left, right + postfix.end()), (left, right))
    return None


def quantity_expression_spans(text: str, measurement_labels: set[str]) -> list[tuple[int, int]]:
    """为分句提供已识别计数、倍数的完整原文范围，不把换行当作数值。"""
    candidates = lexical_quantity_word_spans(text)
    start = 0
    while start < len(text):
        if not text[start].isnumeric():
            start += 1
            continue
        end = start + 1
        while end < len(text) and text[end].isnumeric():
            end += 1
        candidates.append((start, end))
        start = end
    spans = []
    for left, right in candidates:
        owner = numeric_value_owner(text, left, right, measurement_labels)
        if owner is not None and owner.origin in {"count", "relative_quantity"}:
            spans.append(owner.owner_span)
    return spans


@dataclass(frozen=True)
class StatisticValueEvaluation:
    """一次数值核验及它唯一所属的统计赋值，不靠包含关系共用核验。"""

    owner_span: tuple[int, int]
    value_span: tuple[int, int]


@dataclass(frozen=True)
class StatisticValueSlot:
    """保留统计赋值的原文位置；空值或未知文字同样占据值槽。"""

    label: str
    statement_span: tuple[int, int]
    value_span: tuple[int, int]
    statement_type: Literal["statistic_assignment"] = "statistic_assignment"

    def has_evaluation(self, evaluations: list[StatisticValueEvaluation]) -> bool:
        """只接受本断言直接值的核验，内嵌断言不履行外层断言。"""
        return any(item.owner_span == self.statement_span for item in evaluations)


def statistic_value_owner(
    text: str, span: tuple[int, int], slots: list[StatisticValueSlot]
) -> StatisticValueSlot | None:
    """数值须占据最近赋值的直接值位置；说明文字和嵌套统计不是主值。"""
    owners = [
        slot for slot in slots if slot.value_span[0] <= span[0] < span[1] <= slot.value_span[1]
    ]
    if not owners:
        return None
    owner = max(owners, key=lambda slot: slot.statement_span[0])
    prefix = text[owner.value_span[0] : span[0]]
    # Only value wrappers/approximation syntax can precede a direct expression.
    # Arbitrary prose is not erased to borrow a later, separately verified fact.
    if not re.fullmatch(r"\s*(?:(?:约|大约)\s*)?[\s(（「『\"'`“‘]*", prefix):
        return None
    return owner


def statistic_value_slots(
    text: str, measurement_labels: set[str], source_labels: set[str]
) -> list[StatisticValueSlot]:
    """按统计标签与赋值结构识别值槽，不枚举值的语言或字母表。"""
    labels = "|".join(
        re.escape(label) for label in sorted(measurement_labels, key=len, reverse=True)
    )
    assignment = _assignment_pattern(labels)
    protected = [
        match.span()
        for label in source_labels - measurement_labels
        for match in re.finditer(re.escape(label), text)
    ]
    slots = []
    for match in assignment.finditer(text):
        if any(a <= match.start() < match.end() <= b for a, b in protected):
            continue
        # A known statistic may be the suffix of a quoted subject. Only a
        # real closing delimiter can end that label before its assignment.
        if match["label_close"] and not _closes_quote_at(text, match.start("label_close")):
            continue
        start = match.end()
        end = len(text)
        for boundary in re.finditer(r"[，,。；;!?！？]", text[start:]):
            index = start + boundary.start()
            if (
                boundary[0] == ","
                and index > start
                and index + 1 < len(text)
                and text[index - 1].isdigit()
                and text[index + 1].isdigit()
            ):
                continue
            end = index
            break
        while start < end and text[start].isspace():
            start += 1
        if _closes_quote_at(text, start):
            continue
        while end > start and text[end - 1].isspace():
            end -= 1
        slots.append(StatisticValueSlot(match[0], (match.start(), end), (start, end)))
    return slots


def paired_quote_spans(text: str) -> list[tuple[int, int, int, int]]:
    """原文中的完整引号对，内层先返回；反引号以完整连续宽度匹配。"""
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}
    stack: list[tuple[str, int, int]] = []
    closed: list[tuple[int, int, int, int]] = []
    index = 0
    while index < len(text):
        char = quote_token_at(text, index)
        if char is None:
            index += 1
            continue
        width = 1
        if char == "`":
            while index + width < len(text) and text[index + width] == "`":
                width += 1
            char = "`" * width
        if stack and char == stack[-1][0]:
            _, opening, opening_width = stack.pop()
            closed.append((opening, opening + opening_width, index, index + width))
        elif char in pairs or char.startswith("`"):
            stack.append((pairs.get(char, char), index, width))
        index += width
    return closed
