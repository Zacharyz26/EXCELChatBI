"""区分工具统计解释与有作用域的知识检索请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.orchestrator.control.claim_structure import delimited_spans

_ACTION = re.compile(r"执行|查询|检索|搜索|查阅|查找|查|解释|说明|根据|依据|给出|提供")
_SEARCH = re.compile(r"查询|检索|搜索|查阅|查找|查")
_SOURCE = re.compile(r"知识库|公司规定|公司手册|业务文档|规章|制度")
_STATISTIC = re.compile(r"统计|计数|重复|空值|非空|缺失|不同值|记录数|组数|工具|画像")
_BUSINESS = re.compile(r"公司|业务|企业|组织|营收|收入|利润|营业额|销售额|规定|制度")
_NEGATIONS = {
    "不需要",
    "不可能",
    "无需",
    "不用",
    "不必",
    "不要",
    "勿",
    "切勿",
    "不得",
    "不能",
    "禁止",
    "并非",
    "不是",
    "并不",
    "别",
    "不",
}
_MODIFIERS = {
    "改口要求",
    "改口为",
    "更改为",
    "调整为",
    "改为",
    "改成",
    "请",
    "再",
    "去",
    "进行",
    "继续",
    "需要",
    "必须",
    "务必",
    "应该",
    "应当",
    "可以",
    "要",
    "能",
}
_TEMPORAL_WORDS = {
    "本次",
    "本轮",
    "现在",
    "目前",
    "暂时",
    "暂",
    "当前",
    "这里",
    "这次",
    "刚才",
    "之前",
    "先",
    "也",
    "仍",
    "依然",
}
_POLARITY_WORDS = tuple(
    sorted(_NEGATIONS | _MODIFIERS | _TEMPORAL_WORDS, key=lambda s: (-len(s), s))
)


@dataclass(frozen=True)
class _Clause:
    """原始对象拼写与屏蔽引用后的指令视图共享位置。"""

    raw: str
    text: str


@dataclass(frozen=True)
class _QueryScope:
    # Each node is a complete literal name, not a fragment of a flattened title.
    path: tuple[str, ...]
    all_sources: bool = False


@dataclass(frozen=True)
class _Directive:
    scope: _QueryScope
    prohibited: bool


def _paired_examples(text: str) -> str:
    # Only a paired, explicit prose-example block is data. An unclosed label is
    # not permission to silently discard all later requests.
    lines = text.splitlines(keepends=True)
    start: int | None = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"\s*(?:以下|下面)(?:只是|仅为|是)?(?:示例|例子|引用)[:：]\s*", line):
            if start is None:
                start = i
        elif start is not None and re.fullmatch(
            r"\s*(?:以上|上述)(?:不是本次请求|仅为示例|只是示例)[。.]?\s*", line
        ):
            lines[start : i + 1] = ["\n"] * (i - start + 1)
            start = None
    return "".join(lines)


def _request_clauses(text: str) -> list[_Clause]:
    # Quoted objects remain opaque: their verbs/negations are not requests.
    visible: list[str] = []
    fence: tuple[str, int] | None = None
    quotation = False
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = (marker[1][0], len(marker[1]))
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and re.fullmatch(r" {0,3}" + re.escape(marker[1]) + r"\s*", line)
            ):
                fence = None
            visible.append("\n")
            continue
        if not line.strip():
            quotation = False
        if line.lstrip().startswith(">"):
            quotation = True
        visible.append("\n" if fence or quotation else line)
    source = _paired_examples("".join(visible))
    masked = list(source)
    for a, b in delimited_spans(source):
        masked[a:b] = "§" * (b - a)
    view = "".join(masked)
    separator = re.compile(
        r"[，,。；;!?！？\n]|但(?:是)?|另外|同时|并(?:且)?(?=请|查询|检索|搜索|查阅|解释|说明)"
    )
    clauses: list[_Clause] = []
    start = 0
    for end in [*separator.finditer(view), None]:
        stop = end.start() if end else len(view)
        section = view[start:stop]
        left = start + len(section) - len(section.lstrip())
        right = start + len(section.rstrip())
        if left < right:
            clauses.append(_Clause(source[left:right], view[left:right]))
        start = end.end() if end else len(view)
    return clauses


def _predicate_polarity(prefix: str) -> tuple[bool, str]:
    """Compose adjacent operators; return negation and the remaining subject.

    A non-operator such as 遗漏/忘记 terminates the chain. Unrecognized prose is
    not evidence of a prohibition, so uncertain business requests remain planned.
    """
    end = len(prefix)
    negative = False
    while end:
        while end and prefix[end - 1].isspace():
            end -= 1
        word = next((w for w in _POLARITY_WORDS if prefix.endswith(w, 0, end)), None)
        if word is None:
            break
        negative ^= word in _NEGATIONS
        end -= len(word)
    return negative, prefix[:end]


def _explains_search_predicate(action: str, target: str) -> bool:
    if action not in {"解释", "说明"}:
        return False
    search = re.search(f"{_SEARCH.pattern}|{_CANCEL.pattern}", target)
    if search is None:
        return False
    prefix = target[: search.start()]
    reason = re.match(r"^(?:为什么|为何)", prefix)
    _, subject = _predicate_polarity(prefix[reason.end() :] if reason else prefix)
    # A business subject before the embedded action still asks about its policy.
    return _context_only(subject) and bool(
        reason or re.search(r"(?:的原因|的理由|的含义|是否必要|是否需要)\s*$", target)
    )


_CONTEXT_WORDS = tuple(
    sorted(_TEMPORAL_WORDS | {"请", "对", "将", "把"}, key=lambda s: (-len(s), s))
)
_REVISIONS = ("改口要求", "改口为", "更改为", "调整为", "改为", "改成")
_CANCEL = re.compile(r"取消|撤销|解除|撤回|收回")
_NAME_PAIRS = dict(zip("“‘「『《〈\"'`", "”’」』》〉\"'`", strict=True))


def _without_context(text: str) -> str:
    text = text.strip()
    start = 0
    while start < len(text):
        if text[start].isspace():
            start += 1
            continue
        word = next((w for w in _CONTEXT_WORDS if text.startswith(w, start)), None)
        if word is None:
            break
        start += len(word)
    return text[start:].strip()


def _context_only(text: str) -> bool:
    return not _without_context(text)


def _query_scope(text: str) -> _QueryScope:
    # Remove syntax only outside quoted names. A literal 本轮 inside a title
    # stays part of its identity, and literal '*' is never a wildcard.
    text = _without_context(text).strip()
    temporal = sorted(_TEMPORAL_WORDS, key=lambda s: (-len(s), s))
    while text:
        word = next((w for w in temporal if text.endswith(w)), None)
        if word is None:
            break
        text = text[: -len(word)].rstrip()
    text = text.removesuffix("的").rstrip()
    text = re.sub(r"^知识库(?:中的|的|中)", "", text)
    path = _scope_path(text)
    return _QueryScope(path, all_sources=path == ("知识库",))


def _literal_scope_name(text: str) -> str:
    parts: list[str] = []
    start = 0
    for a, b in delimited_spans(text):
        parts.append(text[start:a])
        literal = text[a:b]
        if len(literal) >= 2 and _NAME_PAIRS.get(literal[0]) == literal[-1]:
            literal = literal[1:-1].strip()
        parts.append(literal)
        start = b
    parts.append(text[start:])
    return "".join(parts).strip()


def _scope_path(text: str) -> tuple[str, ...]:
    # Determine external relations BEFORE unwrapping titles. Internal 的/中 are
    # literal name characters; they can never become parent/child boundaries.
    view = list(text)
    after_title: set[int] = set()
    for a, b in delimited_spans(text):
        view[a:b] = "§" * (b - a)
        if b - a >= 2 and _NAME_PAIRS.get(text[a]) == text[b - 1]:
            while b < len(text) and text[b].isspace():
                b += 1
            after_title.add(b)
    path: list[str] = []
    start = 0
    for relation in re.finditer(r"中的|的|中", "".join(view)):
        # Bare 中 needs an explicit preceding title boundary. Otherwise a name
        # such as 公司规定中国版 must not become a member of 公司规定.
        if relation[0] == "中" and relation.start() not in after_title:
            continue
        path.append(_literal_scope_name(text[start : relation.start()]))
        start = relation.end()
    path.append(_literal_scope_name(text[start:]))
    return tuple(path)


def _covers(restriction: _QueryScope, request: _QueryScope) -> bool:
    # Compare whole structural nodes. Similar title strings alone never imply
    # containment; only parsed external member paths admit ancestor coverage.
    return restriction.all_sources or (
        bool(restriction.path)
        and all(restriction.path)
        and restriction.path == request.path[: len(restriction.path)]
    )


class _KnowledgeRequests:
    """按对象折叠请求、禁令与撤回，引用内容永不重新解释为动作。"""

    def __init__(self) -> None:
        self.active: set[_QueryScope] = set()
        self.blocked: set[_QueryScope] = set()
        self.last_query: _QueryScope | None = None
        self.last_restriction: _QueryScope | None = None
        self.last_directive: _Directive | None = None

    def prohibit(self, scope: _QueryScope) -> None:
        self.blocked.add(scope)
        self.active = {q for q in self.active if not _covers(scope, q)}
        self.last_query = self.last_restriction = scope
        self.last_directive = _Directive(scope, True)

    def request(self, scope: _QueryScope, *, revision: bool = False) -> None:
        if revision:
            previous = self.last_query
            if previous is not None:
                self.active = {q for q in self.active if not _covers(previous, q)}
            # Replacement admits only this target, preserving other bans.
            self.active.add(scope)
        elif not any(_covers(b, scope) for b in self.blocked):
            self.active.add(scope)
        self.last_query = scope
        self.last_directive = _Directive(scope, False)

    def cancel(self, directive: _Directive | None) -> None:
        if directive is not None:
            if directive.prohibited:
                self.blocked.discard(directive.scope)
            else:
                self.active = {q for q in self.active if not _covers(directive.scope, q)}
        self.last_restriction = None
        self.last_directive = None
        # Neither lifting a ban nor withdrawing a request adds a new request.


def _directive_target(clause: _Clause, state: _KnowledgeRequests) -> _Directive | None:
    text = _without_context(clause.text)
    offset = len(clause.text.rstrip()) - len(text)
    raw = clause.raw[offset : offset + len(text)]
    reference = re.fullmatch(
        r"(?:这条|该|上述|前述|之前的)(?:(禁止|不)?(查询|检索|搜索|查阅|查找)(?:的)?)?"
        r"(限制|禁令|要求|请求|任务)",
        text,
    )
    if reference:
        if reference[1] or reference[3] in {"限制", "禁令"}:
            scope = state.last_restriction
            return _Directive(scope, True) if scope is not None else None
        previous = state.last_directive
        if (
            previous is not None
            and previous.prohibited
            and (reference[2] or reference[3] in {"请求", "任务"})
        ):
            # A typed positive request reference cannot lift a prohibition.
            return None
        return previous
    named = re.fullmatch(r"(.+?)(?:的)?(限制|禁令|要求|请求|任务)", text)
    if named is None:
        return None
    subject, noun = named[1], named[2]
    raw_subject = raw[: named.end(1)]
    action = _SEARCH.search(subject)
    if action is None:
        scope = _query_scope(raw_subject)
        return _Directive(scope, True) if scope.all_sources and noun in {"限制", "禁令"} else None
    negative, before = _predicate_polarity(subject[: action.start()])
    after = subject[action.end() :].strip()
    if not (_SOURCE.search(subject) or re.search(r"定义|口径", subject) or "§" in subject):
        return None
    if after and not _context_only(before):
        return None
    target = raw_subject[action.end() :] if after else raw_subject[: len(before)]
    return _Directive(_query_scope(target), negative or noun in {"限制", "禁令"})


def _cancel_directive(clause: _Clause, state: _KnowledgeRequests) -> bool:
    verb = _CANCEL.search(clause.text)
    if verb is None:
        return False
    negative, subject = _predicate_polarity(clause.text[: verb.start()])
    target = clause.text[verb.end() :].strip()
    if target:
        if not _context_only(subject):
            action = _ACTION.search(clause.text)
            # A conditional/unknown cancellation prefix cannot fall through and
            # turn the embedded search in its object into a new search request.
            return action is None or verb.start() < action.start()
        raw_target = clause.raw[verb.end() :]
        target = clause.text[verb.end() :]
    else:
        raw_target = clause.raw[: len(subject)]
        target = subject
    if not negative:
        state.cancel(_directive_target(_Clause(raw_target, target), state))
    return True


def _explicit_revision(prefix: str) -> bool:
    negative, subject = _predicate_polarity(prefix)
    return not negative and _context_only(subject) and any(w in prefix for w in _REVISIONS)


def knowledge_search_requested(user_text: str) -> bool:
    """按外层指令、规范对象与撤回方向保留仍然有效的知识查询。"""
    clauses = _request_clauses(user_text)
    profile_context = any(_STATISTIC.search(c.text) for c in clauses)
    state = _KnowledgeRequests()
    for parsed in clauses:
        clause = parsed.text
        if _cancel_directive(parsed, state):
            continue
        action = _ACTION.search(clause)
        definition = re.search(r"定义|口径|公司规定", clause)
        source = _SOURCE.search(clause)
        if action:
            prefix, target = clause[: action.start()], clause[action.end() :]
            negative, subject = _predicate_polarity(prefix)
            searching = _SEARCH.fullmatch(action[0]) is not None
            query = searching and bool(source or definition or "§" in target or "§" in subject)
            raw_target = (
                parsed.raw[action.end() :] if target.strip() else parsed.raw[: len(subject)]
            )
            if negative:
                if query:
                    state.prohibit(_query_scope(raw_target))
                else:
                    state.last_restriction = None
                    state.last_directive = None
                continue
            if _explains_search_predicate(action[0], target):
                continue
            if query:
                state.request(_query_scope(raw_target), revision=_explicit_revision(prefix))
                continue
            state.last_directive = None
        elif definition:
            target = clause
            raw_target = parsed.raw
            if _predicate_polarity(clause[: definition.start()])[0]:
                state.last_restriction = None
                state.last_directive = None
                continue
        else:
            state.last_directive = None
            if any(word in clause for word in _NEGATIONS):
                state.last_restriction = None
            continue
        if definition or source:
            if _BUSINESS.search(target):
                state.request(_query_scope(raw_target))
            elif not _STATISTIC.search(target):
                object_text = re.split(r"定义|口径", target, maxsplit=1)[0].strip(" 的")
                if object_text or not profile_context:
                    state.request(_query_scope(raw_target))
    return bool(state.active)
