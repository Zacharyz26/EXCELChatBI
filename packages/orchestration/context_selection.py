"""有限上下文选择语法和冻结义务；不读取 Session、模型或运行数据库。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from packages.common.text_surface import parse_text_surface
from packages.orchestration.operation_intent import (
    DATA_OPERATION_ACTIONS,
    EXECUTION_ACTIONS,
    operation_prefix_disposition,
)

if TYPE_CHECKING:
    from packages.orchestration.contracts import TaskContract


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    """显式肯定来源语法，仅证明意图；访问权由 Host/SQLite 来源另证。"""

    dataset_ref: str
    source_span: tuple[int, int]
    selection_only: bool
    reason_code: str | None = None
    command_spans: tuple[tuple[int, int], ...] = ()
    ignored_reference_spans: tuple[tuple[int, int], ...] = ()


_RESOURCE = r"dataset[_ ]?ref\s*[:=]\s*(?P<ref>[0-9a-f]{32})(?![0-9a-fA-F_])"
_SOURCE_VERBS = frozenset({"切换到", "使用", "选择", "采用", "改用", "切换", "用"})
# Consume complete shared execution tokens first. A suffix inside another
# lexical action is not an independent source action (e.g. 应用 -> 用).
_SOURCE_ACTION = re.compile(
    "|".join(sorted(_SOURCE_VERBS | set(EXECUTION_ACTIONS), key=lambda word: (-len(word), word)))
)
_CURRENT_OBJECT = re.compile(r"它|该数据集|这个数据集|此数据集|当前数据集|该来源|此来源")
_CHOICE_OBJECT = re.compile(r"(?:(?:该|这个|此前|之前|刚才|本次|本轮)(?:的)?)?(?:数据|来源)?选择")

_BUSINESS_VERBS = frozenset(
    (
        *DATA_OPERATION_ACTIONS,
        *EXECUTION_ACTIONS,
        "生成",
        "绘制",
        "查看",
        "检查",
        "统计",
        "分析",
        "总结",
    )
)
_BUSINESS_CONTINUATION = re.compile(
    "|".join(sorted(_BUSINESS_VERBS, key=lambda word: (-len(word), word)))
)
_NAMED_OPERATION_ACTION = re.compile(
    "|".join(sorted(_SOURCE_VERBS | _BUSINESS_VERBS, key=lambda word: (-len(word), word)))
)


@dataclass(frozen=True, slots=True)
class NamedSourceUse:
    name: str
    dataset_ref: str
    span: tuple[int, int]
    disposition: str


def _named_source_uses(
    text: str, sources: tuple[tuple[str, str], ...]
) -> tuple[NamedSourceUse, ...]:
    """可信名称只提供身份；用途来自同一原文结构/完整动作前缀。"""
    if not sources:
        return ()
    mapping = dict(sources)
    names = sorted(mapping, key=lambda name: (-len(name), name))
    pattern = re.compile("|".join(re.escape(name) for name in names))
    surface = parse_text_surface(text)
    protected = [
        node.span
        for node in surface.nodes
        if node.kind in {"quote", "inline_code", "fence", "blockquote"}
    ]
    result = []
    for match in pattern.finditer(text):
        name = match[0]
        disposition = "unknown"
        if any(left <= match.start() and match.end() <= right for left, right in protected):
            disposition = "quoted"
        else:
            tail = text[match.end() :]
            boundary = (
                not tail
                or tail[0].isspace()
                or tail[0] in "。.!！?？;；,，:：）)]}】"
                or _BUSINESS_CONTINUATION.match(tail) is not None
            )
            left = max(
                (m.end() for m in re.finditer(r"[。.!！?？;；,，]|\n\s*\n", text[: match.start()])),
                default=0,
            )
            prefix = text[left : match.start()]
            # A complete accepted business/source verb owns this operand.
            # All such verbs share polarity; merely mentioning a name does not
            # authorize a source, even when its identity is already governed.
            actions = [
                action
                for action in _NAMED_OPERATION_ACTION.finditer(prefix)
                if not prefix[action.end() :].strip()
            ]
            if actions:
                action_prefix = prefix[: actions[-1].start()].strip()
                disposition = (
                    "current"
                    if re.fullmatch(r"(?:请\s*)?继续", action_prefix)
                    else operation_prefix_disposition(action_prefix) or "unknown"
                )
            if not boundary:
                disposition = "unknown"
        result.append(NamedSourceUse(name, mapping[name], match.span(), disposition))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class NamedSelectionProof:
    """已治理 Dataset 名称的纯来源选择；不代替 live Memory 生命周期检查。"""

    name: str
    span: tuple[int, int]
    dataset_ref: str
    memory_id: str
    memory_binding_hash: str

    def to_dict(self) -> dict[str, str | list[int]]:
        return {
            "name": self.name,
            "span": list(self.span),
            "dataset_ref": self.dataset_ref,
            "memory_id": self.memory_id,
            "memory_binding_hash": self.memory_binding_hash,
        }

    @classmethod
    def from_dict(cls, raw: object, *, query: str, dataset_ref: str) -> NamedSelectionProof:
        if not isinstance(raw, dict) or set(raw) != {
            "name",
            "span",
            "dataset_ref",
            "memory_id",
            "memory_binding_hash",
        }:
            raise ValueError("命名选择证明结构无效")
        name, span = raw["name"], raw["span"]
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 80
            or not isinstance(span, list)
            or len(span) != 2
            or any(type(value) is not int for value in span)
            or not 0 <= span[0] < span[1] <= len(query)
            or query[span[0] : span[1]] != name
            or raw["dataset_ref"] != dataset_ref
            or not isinstance(raw["memory_id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", raw["memory_id"]) is None
            or not isinstance(raw["memory_binding_hash"], str)
            or re.fullmatch(r"[0-9a-f]{64}", raw["memory_binding_hash"]) is None
        ):
            raise ValueError("命名选择证明身份或原文位置无效")
        intent = source_intent(query, named_sources=((name, dataset_ref),))
        if (
            intent.reason_code is not None
            or intent.selection is None
            or not intent.selection.selection_only
            or len(intent.named_operands) != 1
            or intent.named_operands[0].span != tuple(span)
        ):
            raise ValueError("命名选择不是完整的纯来源命令")
        return cls(
            name, (span[0], span[1]), dataset_ref, raw["memory_id"], raw["memory_binding_hash"]
        )


def _named_selection_only(text: str, uses: tuple[NamedSourceUse, ...]) -> DatasetSelection | None:
    if len(uses) != 1 or uses[0].disposition != "current":
        return None
    use = uses[0]
    prefix = text[: use.span[0]]
    suffix = text[use.span[1] :]
    if (
        re.fullmatch(r"\s*(?:请\s*)?(?:继续\s*)?(?:使用|选择|采用|切换(?:到)?|改用|用)\s*", prefix)
        is None
        or re.fullmatch(r"\s*[。.!！]?\s*", suffix) is None
    ):
        return None
    return DatasetSelection(
        use.dataset_ref, (0, use.span[1]), True, command_spans=((0, use.span[1]),)
    )


def _instruction_surface(text: str) -> tuple[str, tuple[tuple[int, int], ...], bool]:
    surface = parse_text_surface(text)
    view = list(text)
    for node in surface.nodes:
        if node.kind in {"quote", "inline_code", "fence", "blockquote"}:
            left, right = node.span
            view[left:right] = [" " if c not in "\r\n" else c for c in text[left:right]]
    protected = tuple(node.span for node in surface.nodes if node.kind == "bracket")
    return "".join(view), protected, any(not node.closed for node in surface.nodes)


def _source_object(text: str, selected: str | None) -> str | None:
    raw = text.strip()
    if _CURRENT_OBJECT.fullmatch(raw) or _CHOICE_OBJECT.fullmatch(raw):
        return selected
    prefix = r"(?:数据集)?\s*"
    for pattern in (
        prefix + _RESOURCE,
        prefix + r"（\s*" + _RESOURCE + r"\s*）",
        prefix + r"\(\s*" + _RESOURCE + r"\s*\)",
    ):
        match = re.fullmatch(pattern, raw, re.I)
        if match:
            return match["ref"].lower()
    return None


def _source_object_candidate(text: str) -> bool:
    """仅决定未知是否属于来源域，不由这些文字授予来源权限。"""
    raw = text.strip()
    return bool(
        _CURRENT_OBJECT.fullmatch(raw)
        or _CHOICE_OBJECT.fullmatch(raw)
        or re.search(_RESOURCE, raw, re.I)
        or re.search(r"数据集|来源", raw)
    )


@dataclass(frozen=True, slots=True)
class SourceClause:
    """完整来源子句的原文归属；引用不是执行许可。"""

    span: tuple[int, int]
    dataset_ref: str | None
    disposition: str


@dataclass(frozen=True, slots=True)
class SourceIntent:
    """即使没有肯定选择，也保存禁止、未知及引用边界。"""

    selection: DatasetSelection | None
    reason_code: str | None
    clauses: tuple[SourceClause, ...]
    ignored_reference_spans: tuple[tuple[int, int], ...]
    named_operands: tuple[NamedSourceUse, ...] = ()

    @property
    def dataset_ref(self) -> str | None:
        if self.selection is not None:
            return self.selection.dataset_ref
        refs = {item.dataset_ref for item in self.clauses if item.dataset_ref is not None}
        return next(iter(refs)) if len(refs) == 1 else None


def _source_clause(clause: str, selected: str | None, span: tuple[int, int]) -> SourceClause | None:
    """只解析完整来源命令，复用操作前缀；不依任意词出现授权。"""
    clean = clause.strip()
    withdrawal = re.fullmatch(r"(?:取消|撤回)(?:对(?P<target>.+?)的选择|(?P<choice>.+))", clean)
    if withdrawal:
        target = withdrawal["target"] or withdrawal["choice"]
        owner = _source_object(target, selected)
        if owner is None and not _source_object_candidate(target):
            return None
        return SourceClause(span, owner, "cancelled" if owner is not None else "unknown")
    for action in _SOURCE_ACTION.finditer(clean):
        if action[0] not in _SOURCE_VERBS:
            continue
        prefix, target = clean[: action.start()], clean[action.end() :]
        disposition = operation_prefix_disposition(prefix)
        owner = _source_object(target, selected)
        if disposition == "described":
            continue
        if owner is None and not _source_object_candidate(target):
            continue
        return SourceClause(
            span,
            owner,
            disposition if disposition is not None and owner is not None else "unknown",
        )
    return None


def _clause_effect(command: SourceClause | None, selected: str) -> str | None:
    if command is None:
        return None
    if command.disposition == "unknown":
        return "source_selection_unknown"
    if command.disposition == "current":
        return None if command.dataset_ref == selected else "source_selection_conflict"
    return "source_selection_conflict" if command.dataset_ref == selected else None


def _positive_dataset_selection(
    text: str, current_dataset_ref: str | None = None
) -> DatasetSelection | None:
    """在共同原源结构上判定有限完整选择/撤回子句，不扫描任意尾部关键词。"""
    view, protected, unclosed = _instruction_surface(text)
    selector = (
        r"\s*(?:(?:接下来|后续|之后)(?:的分析)?\s*)?(?:请\s*)?(?:继续\s*)?"
        r"(?:使用|选择|采用|切换(?:到)?)\s*(?:数据集)?\s*"
    )
    candidates = [
        (selector + r"（\s*" + _RESOURCE + r"\s*）", True),
        (selector + r"\(\s*" + _RESOURCE + r"\s*\)", True),
        (selector + _RESOURCE, True),
        (r"\s*" + _RESOURCE, False),
    ]
    if current_dataset_ref is not None:
        candidates.append(
            (selector + r"(?P<current>当前数据集|该数据集|这个数据集|此数据集|它)", True)
        )
    for pattern, explicit_verb in candidates:
        match = re.match(pattern, view, re.I)
        if match is None:
            continue
        tail = view[match.end() :]
        if (
            tail
            and re.match(r"(?:\s+|[。.!！;；,，])", tail) is None
            and _BUSINESS_CONTINUATION.match(tail) is None
        ):
            continue
        selected = match["ref"].lower() if "ref" in match.re.groupindex else current_dataset_ref
        assert selected is not None
        reason = "source_selection_structure_unclosed" if unclosed else None
        boundaries = [match.end()]
        for delimiter in re.finditer(r"[。.!！?？;；,，]|\n\s*\n", view[match.end() :]):
            left, right = match.end() + delimiter.start(), match.end() + delimiter.end()
            if any(a <= left < b for a, b in protected):
                continue
            boundaries.extend((left, right))
        boundaries.append(len(text))
        commands: list[tuple[int, int]] = [match.span()]
        ignored_references = [
            node.span
            for node in parse_text_surface(text).nodes
            if node.kind in {"quote", "inline_code", "fence", "blockquote"}
            and re.search(_RESOURCE, text[node.span[0] : node.span[1]], re.I)
        ]
        # Alternating content/delimiter endpoints preserve original spans.
        for left, right in zip(boundaries[::2], boundaries[1::2], strict=True):
            raw = view[left:right]
            if not raw.strip():
                continue
            command = _source_clause(raw, selected, (left, right))
            effect = _clause_effect(command, selected)
            commands.append((left, right))
            # A complete withdrawal of a different exact Dataset is not a
            # positive target. Keep the span, so coref cannot promote its ID.
            if (
                command is not None
                and command.disposition not in {"current", "unknown"}
                and command.dataset_ref != selected
            ):
                ignored_references.append((left, right))
            reason = reason or effect
        only = explicit_verb and re.fullmatch(r"\s*[。.!！]?\s*", text[match.end() :]) is not None
        return DatasetSelection(
            selected,
            match.span(),
            only and reason is None,
            reason,
            tuple(commands),
            tuple(sorted(set(ignored_references))),
        )
    return None


def source_intent(
    text: str,
    *,
    current_dataset_ref: str | None = None,
    named_sources: tuple[tuple[str, str], ...] = (),
) -> SourceIntent:
    """共同源意图总返回；None 不再让否定或引文重入旧 ID 路径。"""
    surface = parse_text_surface(text)
    ignored = tuple(
        node.span
        for node in surface.nodes
        if node.kind in {"quote", "inline_code", "fence", "blockquote"}
        and re.search(_RESOURCE, text[node.span[0] : node.span[1]], re.I)
    )
    view, protected, unclosed = _instruction_surface(text)
    named_uses = _named_source_uses(text, named_sources)
    selection = _positive_dataset_selection(text, current_dataset_ref) or _named_selection_only(
        text, named_uses
    )
    selected = selection.dataset_ref if selection is not None else current_dataset_ref
    start = selection.source_span[1] if selection is not None else 0
    spans: list[tuple[int, int]] = []
    left = start
    for delimiter in re.finditer(r"[。.!！?？;；,，]|\n\s*\n", view[start:]):
        a, z = start + delimiter.start(), start + delimiter.end()
        if any(p <= a < q for p, q in protected):
            continue
        spans.append((left, a))
        left = z
    spans.append((left, len(text)))
    clauses: list[SourceClause] = []
    if selection is not None:
        clauses.append(SourceClause(selection.source_span, selected, "current"))
    for span in spans:
        clause = _source_clause(view[span[0] : span[1]], selected, span)
        if clause is not None:
            clauses.append(clause)
    reason = selection.reason_code if selection is not None else None
    if selection is not None:
        ignored = tuple(sorted(set((*ignored, *selection.ignored_reference_spans))))
    elif clauses:
        # There is a source command, but no uniquely supported affirmative
        # leading selector. A negative or an unknown object is never an ID
        # authorization and never silently reuses the previous active source.
        reason = (
            "source_selection_unknown"
            if any(item.disposition in {"current", "unknown"} for item in clauses)
            else "source_selection_conflict"
        )
    elif not view.strip(" \t\r\n。.!！?？;；,，") and any(
        re.search(_RESOURCE, text[a:z], re.I) for a, z in ignored
    ):
        # A whole quoted/fenced resource has no outer executable request.
        reason = "source_selection_quoted"
    if unclosed and (selection is not None or clauses):
        reason = "source_selection_structure_unclosed"
    return SourceIntent(selection, reason, tuple(clauses), ignored, named_uses)


def explicit_dataset_selection(
    text: str, *, current_dataset_ref: str | None = None
) -> DatasetSelection | None:
    """兼容的肯定选择投影；授权消费者须使用完整 source_intent。"""
    return source_intent(text, current_dataset_ref=current_dataset_ref).selection


def contextual_field_requested(text: str, columns: tuple[str, ...] = ()) -> bool:
    """只支持引文之外的显式“该列”；真实同名字段身份优先。"""
    if "该列" in columns:
        return False
    view, _protected, _unclosed = _instruction_surface(text)
    return "该列" in view


def reference_binding_payload(
    query: str,
    targets: Sequence[Mapping[str, object]],
    field_binding: object,
    selection_only: bool,
    named_selection: NamedSelectionProof | None = None,
) -> dict[str, object]:
    """p2 生成/恢复/完成门共用哈希载荷；旧绑定不增加 null 字段。"""
    value: dict[str, object] = {
        "policy_version": "coref-policy-v2",
        "query_hash": hashlib.sha256(query.encode()).hexdigest(),
        "targets": [dict(target) for target in targets],
        "field_binding": field_binding,
        "selection_only": selection_only,
    }
    if named_selection is not None:
        value["named_selection"] = named_selection.to_dict()
    return value


def frozen_reference_dataset(
    goal: str, assumptions: object, expected_ref: str | None
) -> str | None:
    if not isinstance(assumptions, tuple | list):
        return None
    values = [
        item[len("HOST_COREF_V1:") :]
        for item in assumptions
        if isinstance(item, str) and item.startswith("HOST_COREF_V1:")
    ]
    if len(values) != 1:
        return None
    try:
        value = json.loads(values[0])
        if (
            not isinstance(value, dict)
            or value.get("p") != "2"
            or value.get("q") != hashlib.sha256(goal.encode()).hexdigest()
            or type(value.get("s")) is not bool
            or not isinstance(expected_ref, str)
            or value.get("t") != ["d" + expected_ref]
        ):
            return None
        named = None
        if "n" in value:
            named = NamedSelectionProof.from_dict(value["n"], query=goal, dataset_ref=expected_ref)
            if value["s"] is not True or value.get("f") is not None:
                return None
        payload = reference_binding_payload(
            goal,
            [
                {
                    "kind": "dataset",
                    "reference_id": expected_ref,
                    "artifact_type": None,
                    "analysis_id": None,
                    "dataset_ref": expected_ref,
                }
            ],
            value.get("f"),
            value["s"],
            named,
        )
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return expected_ref if digest == value.get("h") else None
    except (TypeError, ValueError):
        return None


def frozen_named_selection(
    goal: str, assumptions: object, expected_ref: str | None
) -> NamedSelectionProof | None:
    """可选 n 必须和同合同 p2/MEM 证明一致；缺失不升级旧合同。"""
    if not isinstance(assumptions, tuple | list):
        return None
    values = [
        item[len("HOST_COREF_V1:") :]
        for item in assumptions
        if isinstance(item, str) and item.startswith("HOST_COREF_V1:")
    ]
    if len(values) != 1:
        return None
    try:
        value = json.loads(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("来源选择冻结格式无效") from exc
    if not isinstance(value, dict) or "n" not in value:
        return None
    ref = frozen_reference_dataset(goal, assumptions, expected_ref)
    if ref is None:
        raise ValueError("命名选择 p2 证明无效")
    proof = NamedSelectionProof.from_dict(value["n"], query=goal, dataset_ref=ref)
    memory = []
    for item in assumptions:
        if isinstance(item, str) and item.startswith("HOST_MEMORY_REF_V1:"):
            try:
                memory.append(json.loads(item[len("HOST_MEMORY_REF_V1:") :]))
            except (TypeError, ValueError) as exc:
                raise ValueError("命名选择记忆证明无效") from exc
    if (
        len(memory) != 1
        or not isinstance(memory[0], dict)
        or (
            memory[0].get("m") != proof.memory_id or memory[0].get("h") != proof.memory_binding_hash
        )
    ):
        raise ValueError("命名选择与冻结记忆身份不一致")
    return proof


def _unproven_source_purpose(contract: TaskContract) -> bool:
    """来源命令没有已证明选择或业务义务时拒绝；不从未知名称造身份。"""
    view, protected, unclosed = _instruction_surface(contract.goal)
    leading = re.match(r"\s*(?:请\s*)?(?:继续\s*)?(?:使用|选择|采用|切换(?:到)?|改用|用)\s*", view)
    if leading is None:
        return False
    if unclosed:
        return True
    requirements = contract.analysis_requirements
    if requirements is not None and (
        any(operation.disposition == "current" for operation in requirements.operations)
        or requirements.knowledge_queries
    ):
        return False
    if any(
        criterion.kind == "artifact" and criterion.artifact_type in {"chart", "report"}
        for criterion in contract.success_criteria
    ):
        return False
    # A later complete business clause can own the remaining instruction.
    # Action-like strings inside quotations or unknown clause prefixes cannot.
    left = 0
    clauses = []
    for delimiter in re.finditer(r"[。.!！?？;；,，]|\n\s*\n", view):
        if any(a <= delimiter.start() < z for a, z in protected):
            continue
        clauses.append(view[left : delimiter.start()])
        left = delimiter.end()
    clauses.append(view[left:])
    for clause in clauses[1:]:
        for action in _BUSINESS_CONTINUATION.finditer(clause):
            if action[0] in _SOURCE_VERBS:
                continue
            if (
                operation_prefix_disposition(clause[: action.start()]) == "current"
                and clause[action.end() :].strip()
            ):
                return False
    return True


def contract_source_intent(contract: TaskContract) -> SourceIntent:
    req = contract.analysis_requirements
    ref = frozen_reference_dataset(
        contract.goal, contract.assumptions, req.dataset_ref if req else None
    )
    try:
        named = frozen_named_selection(
            contract.goal, contract.assumptions, req.dataset_ref if req else None
        )
    except ValueError:
        return SourceIntent(None, "source_selection_proof_invalid", (), ())
    intent = source_intent(
        contract.goal,
        current_dataset_ref=ref,
        named_sources=((named.name, named.dataset_ref),) if named else (),
    )
    if (
        named is None
        and intent.selection is None
        and intent.reason_code is None
        and _unproven_source_purpose(contract)
    ):
        return SourceIntent(
            None,
            "source_selection_proof_missing",
            intent.clauses,
            intent.ignored_reference_spans,
            intent.named_operands,
        )
    return intent


def selection_only_dataset(contract: TaskContract) -> str | None:
    """复验 Host 冻结选择标识、原文和无其他义务；模型声称不能授权。"""
    selection = contract_source_intent(contract).selection
    req = contract.analysis_requirements
    if selection is None or not selection.selection_only or req is None:
        return None
    req.validate_source(contract.goal)
    if (
        req.dataset_ref != selection.dataset_ref
        or req.clarifications
        or any(op.disposition == "current" for op in req.operations)
        or req.knowledge_queries
        or req.total
        or req.filters
        or any(c.kind != "response" for c in contract.success_criteria)
    ):
        return None
    prefix = "HOST_COREF_V1:"
    assumptions = [item[len(prefix) :] for item in contract.assumptions if item.startswith(prefix)]
    if len(assumptions) != 1:
        return None
    try:
        frozen = json.loads(assumptions[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(frozen, dict) or frozen.get("p") != "2" or frozen.get("s") is not True:
        return None
    if frozen.get("q") != hashlib.sha256(contract.goal.encode()).hexdigest():
        return None
    ref = selection.dataset_ref
    if frozen.get("t") != ["d" + ref] or frozen.get("f") is not None:
        return None
    return (
        ref if frozen_reference_dataset(contract.goal, contract.assumptions, ref) == ref else None
    )
