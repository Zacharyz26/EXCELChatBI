"""统计赋值先形成值槽；字符是否像数字不能决定陈述是否需要核验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import jieba
import jieba.posseg
from packages.common.text_delimiters import quote_token_at
from packages.common.text_surface import quote_nodes

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
COUNT_PREDICATE = (
    r"(?:一共(?:有)?|共有|共计|总共(?:有)?|另有|存在|包含|包括|覆盖|生成|输出|剩余|保留|有)"
)
_COUNT_COUNTERS = {"条", "行", "列", "组", "项", "笔"}
# One assignment operator grammar is consumed by statistic slots and quantity
# ownership. Operators do not certify a value; an unknown right-hand side stays
# an obligation even when its surface form resembles a deictic reference.
ASSIGNMENT_OPERATOR = r"(?:等于|为|是(?!否)|[:：=])"


@dataclass(frozen=True)
class CountExpression:
    """完整数量表达及其宾语；未知表达也保留原始位置。"""

    value_span: tuple[int, int]
    owner_span: tuple[int, int]
    noun: str
    qualified: bool = False
    document_reference: bool = False
    requested_output: bool = False
    referenced_items: int | None = None


def _quantity_context(
    text: str, left: int, noun: str, tail: str
) -> tuple[int, bool, bool, int | None]:
    """局部数量的所有权：数据、直接交付请求、文字结构及明确引用。

    只分类当前数量；同句的其他数字仍须验证。未识别的上下文默认
    是数据义务，不能因整句有“请”、否定词或引号而免除。
    """
    boundary = list(re.finditer(r"[。；;!?！？]|\n[ \t]*\n", text[:left]))
    start = boundary[-1].end() if boundary else 0
    prefix = text[start:left]
    prefix = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", lambda m: " " * len(m[0]), prefix)
    clause = re.split(r"[，,]", prefix)[-1]
    output_verb = r"(?:画|绘制|生成|创建|导出|提供|输出)"
    direct = re.fullmatch(r"\s*请\s*(?:先|再)?\s*" + output_verb + r"\s*", clause)
    inherited = re.match(r"\s*请\s*(?:先|再)?\s*(?:将|把|" + output_verb + ")", prefix)
    requested = bool(direct or (inherited and re.fullmatch(r"\s*" + output_verb + r"\s*", clause)))
    # Document objects belong to the response itself only when a local
    # reference/container or an announcement owns them. A data subject such
    # as 本表有三个指标 is not a document reference.
    document_nouns = {"提示", "说明", "建议", "步骤", "要点", "段落", "章节", "示例"}
    document = noun in document_nouns and bool(
        re.search(r"(?:以下|下列|如下|上述)\s*$", prefix)
        or re.match(r"\s*(?:本|该)?(?:报告|回答|说明)(?:中)?", clause)
    )
    announcement = re.fullmatch(r"\s*(?:顺便|顺带)?(?:提示|说明|列出)\s*", clause)
    # The head of an announced nominal phrase can follow several nominal
    # modifiers. Stop at punctuation or a new number, never search into a
    # subsequent assertion for a convenient document noun.
    nominal = re.split(r"[，,。；;:：!?！？\n\d零〇一二两三四五六七八九十百千万亿]", tail.strip())[
        0
    ]
    # A compound ending in 点 may name observed data points. Neither an
    # introductory verb nor a future-reference colon proves a document list.
    # Keep such ambiguous counts unknown until a structural owner is available.
    if announcement and nominal in document_nouns:
        document = True
    # Deictic references to an immediately preceding, explicitly delimited
    # enumeration count the named labels, not dataset rows. Preserve the
    # cardinality for the consumer to check instead of ignoring any number.
    referenced_items = None
    if re.search(r"(?:这|上述)\s*$", clause) and re.match(
        r"(?:细分)?(?:指标|标签|字段|术语)", tail.strip()
    ):
        stop = re.search(r"(?:这|上述)\s*$", prefix)
        assert stop is not None
        cursor = start + stop.start()
        quotes = {end: begin for begin, _, _, end in paired_quote_spans(text[:cursor])}
        count = 0
        while True:
            while cursor > start and text[cursor - 1].isspace():
                cursor -= 1
            if cursor not in quotes:
                break
            cursor = quotes[cursor]
            count += 1
        if count:
            referenced_items = count
    return start, document, requested, referenced_items


def count_expressions(
    text: str,
    *,
    measurement_labels: set[str] | None = None,
    owner_fields: set[str] | None = None,
) -> list[CountExpression]:
    """数量—量词—宾语先于谓词发现；不靠业务动词或同值 Evidence 发现数量。"""
    quantity = (
        r"(?:[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|"
        r"[零〇一二两三四五六七八九十百千万亿点半几多余]+|若干|许多|少许|很多)"
    )
    pattern = re.compile(
        r"(?P<qualifier>不超过|不低于|至少|至多|超过|不足|大约|约)?"
        r"\s*(?P<amount>" + quantity + r"(?:(?:分之|到|至|/|-)" + quantity + r")?)"
        r"[\s*`\"'“”‘’「」『』)）\]］}｝】〉》]*(?P<counter>[\u3400-\u9fff])"
    )
    expressions = []
    for match in pattern.finditer(text):
        if re.search(r"\n[ \t]*\n", match[0]):
            continue  # Hard paragraphs cannot join an owner to a distant count.
        counter_atoms = _lexical_atoms(match["counter"])
        if match["counter"].isnumeric() or match["counter"] == "两":
            continue  # A numeric suffix is part of an amount, never its classifier.
        if match["counter"] == "号":
            continue  # Ordinal/identifier marker, not a counted business noun.
        if match["counter"] not in _COUNT_COUNTERS and not any(
            tag == "q" for _, tag in counter_atoms
        ):
            compound = _lexical_atoms(match["amount"] + match["counter"])
            if compound != (
                (match["amount"] + match["counter"], "m"),
            ) or _ordinary_numeric_subword(text, *match.span("amount")):
                continue
        left, right = match.span("amount")
        if _nominal_determiner(text, left, right, measurement_labels, owner_fields):
            continue
        if _deictic_member_reference(text, left, right, measurement_labels, owner_fields):
            continue
        if re.search(r"(?:第|每|同|任|某)$", text[:left]):
            continue
        if _relative_ordinal(text, left, right):
            continue
        amount = text[left:right]
        if not amount or not (
            any(c.isnumeric() for c in amount) or re.search(r"两|半|几|若干|许多|少许|很多", amount)
        ):
            continue
        tail = re.split(r"[，,。;；!?！？]|\n[ \t]*\n", text[match.end() :], maxsplit=1)[0]
        # Quotation/bracket wrappers belong to the counted noun, including
        # soft line breaks. They cannot truncate the owner before that noun.
        noun_prefix = re.match(_VALUE_OPEN, tail)
        assert noun_prefix is not None
        noun_tail = tail[noun_prefix.end() :]
        noun_atoms = _lexical_atoms(noun_tail)
        noun = (
            noun_atoms[0][0]
            if noun_atoms and noun_atoms[0][1] not in {"c", "x"}
            else match["counter"]
        )
        owner_end = match.end() + (noun_prefix.end() + len(noun) if noun != match["counter"] else 0)
        # Explicit document deixis counts the response's own structure. This
        # does not exempt data objects or an arbitrary unknown business noun.
        context_start, document_reference, requested_output, referenced_items = _quantity_context(
            text, left, noun, tail
        )
        expressions.append(
            CountExpression(
                (left, right),
                (
                    context_start
                    if document_reference or requested_output or referenced_items is not None
                    else match.start(),
                    owner_end,
                ),
                noun,
                bool(match["qualifier"]),
                document_reference,
                requested_output,
                referenced_items,
            )
        )
    # An explicit counting predicate with a terminal amount is an incomplete
    # quantity assignment, not ordinary wording. Preserve its value with an
    # unknown object; neither a field nor a denominator is inferred. Complete
    # classifier/object expressions above retain their own established role.
    incomplete = re.compile(
        r"(?P<predicate>"
        + COUNT_PREDICATE
        + r")\s*(?:约|大约)?"
        + _VALUE_OPEN
        + r"(?P<amount>"
        + quantity
        + r")"
        + _VALUE_CLOSE
        + r"(?=$|[，,。；;!?！？|\n])"
    )
    for match in incomplete.finditer(text):
        left, right = match.span("amount")
        if (
            any(item.value_span == (left, right) for item in expressions)
            or re.search(r"\n[ \t]*\n", text[match.start() : right])
            or _crosses_quote_close(text, match.start("predicate"), left)
        ):
            continue
        expressions.append(CountExpression((left, right), (match.start(), right), ""))
    expressions.sort(key=lambda item: item.value_span)
    return expressions


def _nominal_determiner(
    text: str,
    left: int,
    right: int,
    measurement_labels: set[str] | None,
    owner_fields: set[str] | None,
) -> bool:
    """词法指代后的单一名词不拆出假量词；真实赋值/存在对象优先保留。"""
    if text[left:right] != "一" or measurement_labels is None:
        return False
    prefix = re.split(r"[，,。；;!?！？]|\n[ \t]*\n", text[:left])[-1]
    prefix_atoms = _lexical_atoms(prefix)
    tail_atoms = _lexical_atoms(text[right : right + 32])
    if not prefix_atoms or prefix_atoms[-1][1] != "r" or not tail_atoms:
        return False
    noun, role = tail_atoms[0]
    if len(noun) <= 1 or role not in {"n", "nz"}:
        return False
    return not (
        any(label and label in prefix for label in measurement_labels)
        or any(field and field in prefix for field in owner_fields or set())
        or re.search(COUNT_PREDICATE + "|" + ASSIGNMENT_OPERATOR, prefix)
    )


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


def _relative_ordinal(text: str, left: int, right: int) -> bool:
    """上/下 + 一 指定相邻序位，不声明集合基数；其后的其他数值仍核验。"""
    if text[left:right] != "一":
        return False
    prefix = re.split(r"[，,。；;!?！？\n]", text[:left])[-1]
    atoms = _lexical_atoms(prefix)
    # A localizer is a complete word. The final character of a result verb
    # such as 剩下/留下 cannot take ownership of the following cardinal.
    return bool(atoms and atoms[-1] in {("上", "f"), ("下", "f")})


def _deictic_member_reference(
    text: str,
    left: int,
    right: int,
    measurement_labels: set[str] | None,
    owner_fields: set[str] | None = None,
) -> bool:
    """指示词加单一成员回指，不是集合基数；必须在名词后立即闭合。

    这/那 + 一 + 单量词/融合量词后接句界才属于回指。“这一个异常”仍有
    被计数对象，不能套用此规则，更不能让相邻的真实业务数量消失。
    """
    if text[left:right] != "一" or not re.search(r"[这那]$", text[:left]):
        return False
    if measurement_labels is None:
        return False  # Without the current statistic grammar, ownership is unknown.
    prefix = re.split(r"[，,。；;!?！？]|\n[ \t]*\n", text[:left])[-1]
    if (
        any(label and label in prefix for label in measurement_labels)
        or any(field and field in prefix for field in owner_fields or set())
        or re.search(COUNT_PREDICATE + "|" + ASSIGNMENT_OPERATOR, prefix)
    ):
        # Known field ownership is only a reason to retain an unknown business
        # quantity, never a new statistic role or proof of its value. This also
        # covers unknown predicates without growing a verb exception list.
        return False
    tail = re.match(r"([\u3400-\u9fff])(?=[，,。；;:：!?！？—]|$)", text[right:])
    if tail is None:
        return False
    # The reference must itself be one lexical demonstrative, not an arbitrary
    # unit following 一. For example a currency unit remains a measurement even
    # when no known field or predicate could be resolved in its prefix.
    reference = text[left - 1] + tail[1]
    atoms = _lexical_atoms(reference)
    return len(atoms) == 1 and atoms[0][0] == reference and atoms[0][1] in {"r", "mq"}


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
        if (offset == left and right < word_end and role != "m") or (
            offset < left < right <= word_end and role in {"d", "r"}
        ):
            return True
        offset = word_end
    return False


def _closes_quote_at(text: str, position: int) -> bool:
    # Reuse actual source quote/code closures. Interior unmatched backtick
    # widths are literal content, not a closing boundary that can erase a slot.
    return any(node.content_span[1] <= position < node.span[1] for node in quote_nodes(text))


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
        + r")(?P<label_close>[」』”’\"'`])?)\s*(?:约|大约)?\s*"
        + ASSIGNMENT_OPERATOR,
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
    if (
        ordinary_word
        or _deictic_member_reference(text, left, right, measurement_labels)
        or re.search(r"(?:同|第|任|每|某)$", prefix)
        or _relative_ordinal(text, left, right)
    ):
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
    counted = count_object_at(text, left, right, measurement_labels=measurement_labels)
    if counted is not None:
        return NumericValueOwner("count", (left, counted[1]), (left, right))
    return None


def count_object_at(
    text: str, left: int, right: int, *, measurement_labels: set[str] | None = None
) -> tuple[str, int] | None:
    """发现有界数量宾语；不以业务对象白名单或 Evidence 同值发现数量。"""
    expression = next(
        (
            item
            for item in count_expressions(text, measurement_labels=measurement_labels)
            if item.value_span == (left, right)
        ),
        None,
    )
    if expression is not None:
        return (
            None
            if expression.document_reference or expression.requested_output
            else (expression.noun, expression.owner_span[1])
        )
    prefix, suffix = text[:left], text[right:]
    if (
        _ordinary_numeric_subword(text, left, right)
        or _deictic_member_reference(text, left, right, measurement_labels)
        or re.search(r"(?:同|第|任|每|某)$", prefix)
        or _relative_ordinal(text, left, right)
    ):
        return None
    predicate = re.search(
        r"(?:共有|共计|总共(?:有)?|另有|存在|包含|包括|覆盖|生成|输出|剩余|保留|有)"
        + _VALUE_OPEN
        + r"$",
        prefix,
    )
    if predicate is None or _crosses_quote_close(text, predicate.start(), left):
        return None
    close = re.match(_VALUE_CLOSE, suffix)
    assert close is not None
    tail = suffix[close.end() :]
    atoms = _lexical_atoms(tail[:48])
    if not atoms:
        return None
    counter, tag = atoms[0]
    if tag != "q" and counter not in _COUNT_COUNTERS:
        # The bundled lexer can emit a quantity and classifier together (半个,
        # 几个). Preserve that entire unknown value rather than losing its noun.
        quantity_atoms = _lexical_atoms(text[left:right])
        if (
            len(quantity_atoms) == 1
            and quantity_atoms[0][1] == "m"
            and re.search(r"[个条行列组项份名次笔]$", text[left:right])
        ):
            return counter, right + close.end() + len(counter)
        return None
    rest = tail[len(counter) :].lstrip()
    nouns = _lexical_atoms(rest[:32])
    if not nouns or nouns[0][0] in "，,。;；!?！？":
        return (
            (counter, right + close.end() + len(counter)) if counter in {"行", "列", "组"} else None
        )
    noun = nouns[0][0]
    return noun, right + close.end() + len(tail) - len(rest) + len(noun)


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
    spans = [
        item.owner_span for item in count_expressions(text, measurement_labels=measurement_labels)
    ]
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
    return [
        (node.span[0], node.content_span[0], node.content_span[1], node.span[1])
        for node in quote_nodes(text)
        if node.closed
    ]
