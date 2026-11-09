"""局部、确定性的当前数据操作意图判定。

同一个动作词可能表示本轮执行、明确禁止、已不存在、延后执行或仅描述
建议/结果。这里不推断任意自然语言，只为受治理的数据筛选与清洗能力提供
共享的、失败关闭的有限语法。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from packages.common.text_delimiters import delimited_spans
from packages.orchestration.report_intent import report_source_intent

OperationDisposition = Literal[
    "current", "prohibited", "absent", "deferred", "described", "cancelled"
]

DATA_OPERATION_ACTIONS = (
    "筛选",
    "过滤",
    "保留",
    "排除",
    "清洗",
    "删除",
    "清理",
    "去除",
    "去掉",
    "移除",
    "去重",
    "汇总",
    "分组",
    "求和",
    "平均",
)
TOTAL_OPERATION_ACTIONS = ("总计", "合计", "总额", "总量", "total")
_ACTION = "|".join((*DATA_OPERATION_ACTIONS, *TOTAL_OPERATION_ACTIONS))
_CONNECTOR = r"(?:、|和|及|以及|或|或者|并(?:且)?|/|／)"
EXECUTION_ACTIONS = ("执行", "应用", "落实", "采用", "实施", "按照", "按", "依照")
_EXECUTE = "|".join(EXECUTION_ACTIONS)
_NEGATIVE_PREFIX = (
    r"暂不|不要|无需|不需要|不必|不用|先别|别|禁止|"
    r"未(?:做|进行)?(?:任何)?|没有(?:做|进行)?(?:任何)?|尚未|并未|先不|不"
)
_DEFERRED_PREFIX = r"以后|后续|下一步|下一轮|下轮|未来|稍后|之后"
_NEGATIVE_GROUP = re.compile(
    rf"(?P<prefix>{_NEGATIVE_PREFIX})"
    rf"\s*(?:再|新增|进行|做|{_EXECUTE})?\s*"
    rf"(?P<body>(?:{_ACTION})(?:\s*{_CONNECTOR}\s*(?:{_ACTION}))*)"
)
_DEFERRED_GROUP = re.compile(
    rf"(?P<prefix>{_DEFERRED_PREFIX})"
    rf"\s*(?:再|可以|可|需要|进行|执行|做)?\s*"
    rf"(?P<body>(?:{_ACTION})(?:\s*{_CONNECTOR}\s*(?:{_ACTION}))*)"
)
_DESCRIPTION = re.compile(rf"(?:{_ACTION})(?:建议|方案|结果|记录|说明|规则|前|后)")
_DESCRIPTION_EXECUTION = re.compile(rf"(?:{_EXECUTE})\s*$")
_FACTUAL_ABSENCE = re.compile(r"^(?:未|没有|尚未|并未)")


@dataclass(frozen=True, slots=True)
class OperationMention:
    """一个动作词及其在原文中的有限语义分类。"""

    action: str
    disposition: OperationDisposition
    span: tuple[int, int]
    context_span: tuple[int, int]


def operation_prefix_disposition(prefix: str) -> OperationDisposition | None:
    """复用现有有限否定/延期语法；未知前缀不默认为肯定操作。"""
    clean = prefix.strip()
    negative = re.fullmatch(
        rf"(?P<prefix>{_NEGATIVE_PREFIX})\s*(?:再|新增|进行|做|{_EXECUTE})?\s*", clean
    )
    if negative:
        return "absent" if _FACTUAL_ABSENCE.match(negative["prefix"]) else "prohibited"
    if re.fullmatch(rf"(?:{_DEFERRED_PREFIX})\s*(?:再|可以|可|需要|进行|执行|做)?", clean):
        return "deferred"
    if re.fullmatch(r"(?:取消|撤回)(?:该|此前|之前|本次|本轮|所有|这个)?\s*", clean):
        return "cancelled"
    if re.fullmatch(r"(?:请\s*)?(?:现在|本次|本轮)?\s*(?:请\s*)?", clean):
        return "current"
    if re.fullmatch(r"(?:请\s*)?(?:建议|说明|区分|描述|介绍|讨论)[^。；;，,]*", clean):
        return "described"
    return None


def _inside(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(left <= index < right for left, right in spans)


def _group_mentions(
    text: str,
    pattern: re.Pattern[str],
    disposition: OperationDisposition,
    ignored: list[tuple[int, int]],
) -> list[OperationMention]:
    mentions: list[OperationMention] = []
    for match in pattern.finditer(text):
        if _inside(match.start(), ignored):
            continue
        group_span = match.span()
        body_start = match.start("body")
        group_disposition = disposition
        if disposition == "prohibited" and _FACTUAL_ABSENCE.match(match["prefix"]):
            group_disposition = "absent"
        for action in re.finditer(_ACTION, match["body"]):
            start = body_start + action.start()
            mentions.append(
                OperationMention(
                    action=action[0],
                    disposition=group_disposition,
                    span=(start, start + len(action[0])),
                    context_span=group_span,
                )
            )
    return mentions


def operation_mentions(
    text: str, *, actions: tuple[str, ...] = ("筛选", "过滤", "排除", "清洗")
) -> tuple[OperationMention, ...]:
    """分类未被引号/括号包裹的有限数据操作词。

    未识别的动作默认视为当前肯定请求；因此不完整或新颖措辞不会因该
    分类器而静默免除已有验证义务。
    """

    # 括号常承载“口径：未筛选”等可核验事实，不能像示例引号一样忽略。
    # 仍复用统一的成对分隔扫描，只挑出真正的引号区域。
    quote_openers = set("\"'`“‘「『")
    ignored = [span for span in delimited_spans(text) if text[span[0]] in quote_openers]
    grouped = _group_mentions(text, _NEGATIVE_GROUP, "prohibited", ignored)
    grouped.extend(_group_mentions(text, _DEFERRED_GROUP, "deferred", ignored))
    occupied = [mention.span for mention in grouped]
    descriptions = [
        match for match in _DESCRIPTION.finditer(text) if not _inside(match.start(), ignored)
    ]
    consumed_sources = report_source_intent(text)
    mentions = list(grouped)
    for match in re.finditer(_ACTION, text):
        if _inside(match.start(), ignored) or _inside(match.start(), occupied):
            continue
        if match[0] == "保留" and not re.search(
            r"(?:[=<>≥≤≠]|等于|大于|小于|为|是)",
            re.split(r"[，,。；;\n]", text[match.end() :], maxsplit=1)[0],
        ):
            # Retaining report material/units is not a data filter. A bounded
            # comparison operand is required before this lexical action exists.
            continue
        description = next(
            (item for item in descriptions if item.start() <= match.start() < item.end()), None
        )
        executes_description = bool(
            description
            and _DESCRIPTION_EXECUTION.search(text[max(0, match.start() - 8) : match.start()])
        )
        disposition: OperationDisposition = (
            "described"
            if consumed_sources.material_owns(*match.span())
            or (description and not executes_description)
            else "current"
        )
        context_span = description.span() if description else match.span()
        mentions.append(
            OperationMention(
                action=match[0],
                disposition=disposition,
                span=match.span(),
                context_span=context_span,
            )
        )
    # Finite local revision/disposition fold. Clauses are not global bans:
    # later explicit revisions affect the same action family only.
    revised: list[OperationMention] = []
    for mention in sorted(mentions, key=lambda item: item.span):
        start = max(
            (m.end() for m in re.finditer(r"[。；;，,\n]|但(?:是)?", text[: mention.span[0]])),
            default=0,
        )
        prefix = text[start : mention.span[0]]
        disposition = mention.disposition
        if re.search(r"(?:取消|撤回)(?:该|此前|之前|本次|本轮|所有|这个)?\s*$", prefix):
            disposition = "cancelled"
        elif disposition == "current" and re.search(
            r"(?:不要|无需|不需要|禁止|不必|不|暂不|先不)[^。；;，,]*$", prefix
        ):
            disposition = "prohibited"
        elif disposition == "current" and re.search(
            r"(?:后续|下一步|下轮|以后|稍后)[^。；;，,]*$", prefix
        ):
            disposition = "deferred"
        elif disposition == "current" and re.search(
            r"(?:建议|说明|区分|描述|介绍|讨论)[^。；;，,]*$",
            re.split(r"然后|接着|随后", prefix)[-1],
        ):
            disposition = "described"
        # Preserve raw mentions. The compiler alone has schema-bound objects
        # with which to safely fold an object-specific withdrawal or revision.
        revised.append(
            OperationMention(
                mention.action,
                disposition,
                mention.span,
                (start, mention.context_span[1])
                if disposition != mention.disposition
                else mention.context_span,
            )
        )
    mentions = revised
    allowed = set(actions)
    return tuple(sorted((m for m in mentions if m.action in allowed), key=lambda m: m.span))


def current_operation_requested(text: str, *, actions: tuple[str, ...]) -> bool:
    """仅当文本包含本轮肯定执行动作时返回真。"""

    return any(
        mention.disposition == "current" for mention in operation_mentions(text, actions=actions)
    )


def operation_family(action: str) -> str:
    """Stable lexical family, not an authorization decision."""
    if action in TOTAL_OPERATION_ACTIONS:
        return "total"
    if action in {"筛选", "过滤", "排除", "保留"}:
        return "filter"
    if action == "去重":
        return "drop_duplicates"
    if action in {"删除", "清理", "去除", "去掉", "移除", "清洗"}:
        return "drop_nulls"
    return "aggregate"
