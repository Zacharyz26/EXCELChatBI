"""数量的来源、文档结构与交付义务：先定所有者，再比较数量。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from packages.orchestration.quality_facts import QUALITY_COUNT_PATH, valid_quality_projection
from packages.session.task_models import EvidenceRecord

from apps.orchestrator.control.count_claims import parse_chinese_count
from apps.orchestrator.control.dataset_identity import (
    identity_declarations,
    identity_matches_binding,
)
from apps.orchestrator.control.numeric_display import NUMBER_PATTERN
from apps.orchestrator.control.profile_population import (
    population_matches,
    requested_population,
    stated_population,
)
from apps.orchestrator.control.statistic_statements import count_expressions, paired_quote_spans

SURFACE_SCHEMA = "quantity-surface-v1"
INSPECTIONS = {
    "review_field_identity": (r"(?:检查|核对)(?:字段名|列名|字段名称)", "检查字段名。", "schema"),
    "review_unit_annotations": (r"(?:检查|核对)(?:单位|单位说明)", "核对单位说明。", "schema"),
    "inspect_data_version": (
        r"(?:查看|核对)(?:数据版本|当前数据版本)",
        "查看数据版本。",
        "version",
    ),
    "retain_source_reference": (
        r"保留(?:(?:原始版本|原始数据)(?:的来源引用)?|来源引用)",
        "保留原始版本的来源引用。",
        "root",
    ),
}


@dataclass(frozen=True)
class SourceTarget:
    """Host 从同一 Evidence 的身份和列投影解析的只读检查目标。"""

    ref: str
    dataset_ref: str
    version: str
    root: str | None
    columns: tuple[str, ...]
    source_hash: str
    scope_binding: dict[str, Any]


def source_binding_matches_goal(binding: dict[str, Any], columns: set[str], goal: str) -> bool:
    """原文与草稿的建议目标共用身份/总体门，不借渲染去掉原始引用。"""
    return all(
        identity_matches_binding(identity, binding) for identity in identity_declarations(goal)
    ) and population_matches(binding, requested_population(goal, columns))


def source_targets(evidence: list[EvidenceRecord], *, goal: str = "") -> list[SourceTarget]:
    """不将模型标题或同值数字用作 schema、版本或根数据集证明。"""
    targets = []
    for record in evidence:
        context = record.summary.get("numeric_context")
        if not isinstance(context, dict):
            continue  # A malformed source cannot provide a trusted identity target.
        ref = context.get("dataset_ref")
        version = context.get("data_version_hash", ref)
        if not isinstance(ref, str) or not ref or not isinstance(version, str) or not version:
            continue
        columns = tuple(
            dict.fromkeys(
                str(i["metric"])
                for i in record.summary.get("value_index", [])
                if isinstance(i, dict)
                and isinstance(i.get("metric"), str)
                and re.fullmatch(r"\$\.profile\.columns\[\d+\]\.\w+", str(i.get("path")))
            )
        )
        projection = record.summary.get("quality_projection")
        if valid_quality_projection(record.summary):
            assert isinstance(projection, dict)
            columns = tuple(projection["columns"])
        if not columns:
            continue
        population = context.get("profile_population")
        binding = {
            "dataset_ref": ref,
            "dataset_version": version,
            "population": population,
            "filters": context.get("filters", []),
        }
        if not source_binding_matches_goal(binding, set(columns), goal):
            continue
        root = (
            population.get("root_ref") if population and population_matches(binding, None) else None
        )
        target_id = hashlib.sha256(
            (record.evidence_id + record.result_hash + ref + version).encode()
        ).hexdigest()
        targets.append(
            SourceTarget(target_id, ref, version, root, columns, record.result_hash, binding)
        )
    return targets


def _plain(text: str) -> str:
    # Keep offsets in all numeric views; punctuation/markup never reassigns facts.
    return re.sub(r"[*_`#]", " ", text)


def _amount(token: str) -> int | None:
    if re.fullmatch(r"\d+", token):
        return int(token)
    return parse_chinese_count(token)


@dataclass(frozen=True)
class Member:
    start: int
    end: int
    text: str
    indent: int
    marker: tuple[int, int]


@dataclass
class SurfaceResult:
    """四门审计与等长数值视图，始终保留原文和每个数量。"""

    source_text: str
    numeric_text: str
    source_hash: str
    quantities: list[dict[str, Any]] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.issues


def _structure(text: str) -> tuple[list[Member], list[tuple[int, int]], bool]:
    members = []
    hidden: list[tuple[int, int]] = []
    fence: tuple[str, int, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                hidden.append((fence[2], offset + len(line)))
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]), offset)
        elif re.match(r"^\s*>", line):
            hidden.append((offset, offset + len(line)))
        else:
            item = re.match(r"^( *)([-+*]|\d+[.)、])\s+(.+?)\s*$", line)
            if item:
                members.append(
                    Member(
                        offset + item.start(3),
                        offset + item.end(3),
                        item[3],
                        len(item[1]),
                        (offset + item.start(2), offset + item.end(2)),
                    )
                )
        offset += len(line)
    if fence:
        hidden.append((fence[2], len(text)))
    return members, hidden, fence is None


def inspection_member(
    text: str, evidence: list[EvidenceRecord], *, goal: str = ""
) -> dict[str, str] | None:
    """完整祈使关系和实际目标共同准入；不按出现某关键词豁免。"""
    plain = _plain(text).strip().rstrip("。.!！").strip()
    targets = source_targets(evidence, goal=goal)
    identities = {(t.dataset_ref, t.version, t.columns, t.root) for t in targets}
    if len(identities) != 1:
        return None
    for action, (grammar, _, target_kind) in INSPECTIONS.items():
        if re.fullmatch(grammar, plain):
            target = targets[0]
            if target_kind == "root" and not target.root:
                return None
            return {"action": action, "target_ref": target.ref, "source_hash": target.source_hash}
    return None


def recommendation_member(
    text: str, evidence: list[EvidenceRecord], *, goal: str = ""
) -> dict[str, Any] | None:
    """完整建议正文只能引用真实 producer 建议；不从动作词或无数字推断。"""
    choices = []
    for record in evidence:
        if not valid_quality_projection(record.summary):
            continue
        if not source_binding_matches_goal(
            _quality_binding(record), set(record.summary["quality_projection"]["columns"]), goal
        ):
            continue
        for advice in record.summary["quality_projection"]["recommendations"]:
            if text.strip() == advice["message"].strip():
                choices.append(
                    {
                        "evidence_id": record.evidence_id,
                        "source_hash": record.result_hash,
                        "issue_id": advice["issue_id"],
                        "action": advice["action"],
                        "message": advice["message"],
                    }
                )
    return choices[0] if len(choices) == 1 else None


def _local(text: str, left: int, right: int) -> tuple[str, int, int]:
    boundaries = list(re.finditer(r"[。；;!?！？]|\n[ \t]*\n", text))
    start = max((m.end() for m in boundaries if m.end() <= left), default=0)
    end = min((m.start() for m in boundaries if m.start() >= right), default=len(text))
    return text[start:end], start, end


def _head_kind(head: str, left: int, right: int) -> str:
    """先判被计数对象，不接收被声明值或列表长度作为分类输入。"""
    prefix, suffix = _plain(head[:left]), _plain(head[right:])
    suffix = re.sub(r"^\s*[)）]*\s*(?:个|项|条)\s*", "", suffix)
    nominal = re.split(r"[，,。；;:：\n]", suffix.strip(), maxsplit=1)[0].strip()
    if re.match(r"(?:已核实|异常|重复|缺失|空值|人员|客户|数据点|记录|行|月份)", nominal):
        return "business"
    if re.search(r"(?:本表|本数据|数据中|存在|发现|已核实|确认了)", prefix + nominal):
        return "business"
    if re.match(r"(?:规则诊断|数据质量诊断|诊断项|诊断)", nominal):
        return "diagnostics"
    if re.match(r"(?:标签|术语|细分指标)(?:[：:]|$|\s)", nominal):
        return "labels"
    writing = re.fullmatch(
        r"\s*(?:(?:顺带|顺便)?(?:提示|说明|列出)|以下|下列|如下|"
        r"本回答按(?:以下|下列|如下)|"
        r"(?:本回答的)?建议列表(?:包含|包括)|"
        r"本回答的?(?:建议)?列表(?:包含|包括))\s*[（(]?\s*",
        prefix,
    )
    if writing and (
        nominal in {"建议", "提示", "列表项", "数据质量点"}
        or re.fullmatch(r"(?:建议|提示|列表项|数据质量点)展开说明", nominal)
    ):
        return "theme" if nominal.startswith("数据质量点") else "document"
    if not nominal and re.fullmatch(r"\s*(?:本回答的)?建议列表(?:包含|包括)\s*", prefix):
        return "document"
    if re.fullmatch(r"\s*这里是\s*", prefix) and nominal.startswith("标签"):
        return "labels"
    return "unknown"


def _quality_binding(record: EvidenceRecord) -> dict[str, Any]:
    c = record.summary["numeric_context"]
    return {
        "dataset_ref": c.get("dataset_ref"),
        "dataset_version": c.get("data_version_hash", c.get("dataset_ref")),
        "population": c.get("profile_population"),
        "filters": c.get("filters", []),
        "statistic": "quality_issue_count",
        "unit": "count",
        "evidence_path": QUALITY_COUNT_PATH,
    }


def _label_members(text: str, left: int, right: int) -> list[list[int]]:
    """只取与明确标签计数相邻的枚举链，旁注和另一名称不能凑数。"""
    quotes = paired_quote_spans(text)
    previous = re.search(r"(?:这|上述)\s*$", text[:left])
    members: list[list[int]] = []
    if previous:
        cursor = previous.start()
        while True:
            gap = re.search(r"[\s、，,]*$", text[:cursor])
            assert gap is not None
            cursor = gap.start()
            quote = next((q for q in quotes if q[3] == cursor), None)
            if quote is None:
                break
            members.insert(0, [quote[1], quote[2]])
            cursor = quote[0]
        return members
    owner = re.match(r"\s*(?:个|项)\s*(?:标签|术语)\s*[:：]\s*", text[right:])
    if owner is None:
        return []
    cursor = right + owner.end()
    while quote := next((q for q in quotes if q[0] == cursor), None):
        members.append([quote[1], quote[2]])
        gap = re.match(r"[\s、，,]*", text[quote[3] :])
        assert gap is not None
        cursor = quote[3] + gap.end()
    return members


def _diagnostic_issue_ids(text: str, record: EvidenceRecord) -> list[str] | None:
    """每条事实先绑定工具来源，再映射诊断身份；不能用同值数或数量挑用途。"""
    from apps.orchestrator.control.claims import _extract_numeric_claims

    refs = [
        r
        for c in _extract_numeric_claims(final_text=text, goal="", evidence=[record])
        for r in c.value_refs
    ]
    if not refs or not all(r.get("supported") is True for r in refs):
        return None
    identities: list[str] = []
    for ref in refs:
        binding = ref.get("semantic_binding", {})
        role = binding.get("statistic")
        column = binding.get("object", {}).get("column")
        owners = {
            issue["issue_id"]
            for issue in record.summary["quality_projection"]["issues"]
            if str(ref.get("path", "")).startswith(issue["path"] + ".")
            or (issue["code"] == "duplicate_rows" and role == "duplicate_rows")
            or (
                issue["code"] in {"missing_values", "all_values_missing"}
                and column in issue["columns"]
                and role in {"null_count", "null_ratio"}
            )
            or (
                issue["code"] == "non_unique_identifier"
                and column in issue["columns"]
                and role in {"duplicate_excess_count", "distinct_count", "non_null_count"}
            )
        }
        if len(owners) != 1:
            return None
        identities.extend(owners)
    return list(dict.fromkeys(identities))


def _diagnostic_members(members: list[Member], record: EvidenceRecord) -> list[str] | None:
    """显式列表每个成员须唯一绑定一个诊断，不能重复成员扩充诊断集合。"""
    identities: list[str] = []
    for member in members:
        owned = _diagnostic_issue_ids(member.text, record)
        if owned is None or len(owned) != 1:
            return None
        identities.extend(owned)
    return identities if len(identities) == len(set(identities)) else None


def _matching_diagnostics(
    head: str, goal: str, evidence: list[EvidenceRecord]
) -> list[EvidenceRecord]:
    return [
        e
        for e in evidence
        if valid_quality_projection(e.summary)
        and all(
            identity_matches_binding(m, _quality_binding(e))
            for m in [*identity_declarations(head), *identity_declarations(goal)]
        )
        and population_matches(
            _quality_binding(e),
            stated_population(head, set(e.summary["quality_projection"]["columns"])),
        )
        and population_matches(
            _quality_binding(e),
            requested_population(goal, set(e.summary["quality_projection"]["columns"])),
        )
    ]


def analyze_surface(
    text: str,
    evidence: list[EvidenceRecord],
    *,
    fact_validator: Callable[[str], bool] | None = None,
    goal: str = "",
) -> SurfaceResult:
    """解析有限源结构；文档证明只消费已核准的成员，业务声明另行绑定。"""
    result = SurfaceResult(text, text, hashlib.sha256(text.encode()).hexdigest())
    if any(
        "numeric_context" in e.summary and not isinstance(e.summary["numeric_context"], dict)
        for e in evidence
    ):
        result.issues.append("invalid_numeric_context")
    view = list(text)
    members, hidden, closed = _structure(text)
    owned_lists: list[tuple[int, int]] = []
    if not closed:
        result.issues.append("structure_incomplete")
    # List markers are source structure, not measurements. Their descendants
    # never change the parent count, and code/quote markers are not author nodes.
    for member in members:
        a, b = member.marker
        view[a:b] = " " * (b - a)
    # Source-authored instruction parameters are not observed business values.
    # Match the complete statement, not a substring inside a model assertion.
    advice_spans = [(m.start, m.end) for m in members]
    advice_spans.extend(
        (m.start(1), m.end(1))
        for m in re.finditer(r"(?:^|\n[ \t]*\n)(.+?)(?=\n[ \t]*\n|$)", text, re.S)
    )
    for start, end in set(advice_spans):
        if any(a <= start < b for a, b in hidden):
            continue
        advice = recommendation_member(text[start:end], evidence, goal=goal)
        if advice is None:
            continue
        for number in NUMBER_PATTERN.finditer(text[start:end]):
            left, right = start + number.start(), start + number.end()
            result.refs.append(
                {
                    "token": number[0],
                    "supported": True,
                    "kind": "advice_parameter",
                    "statement_span": [left, right],
                    "evidence_id": advice["evidence_id"],
                    "advice_proof": advice,
                }
            )
            view[left:right] = " " * (right - left)
    # Reuse the binder's exact source-field identity grammar. Units embedded
    # in a real field (万元) are not a free-standing cardinal measurement.
    from apps.orchestrator.control.profile_claims import profile_field_identity_spans

    fields = {field for target in source_targets(evidence, goal=goal) for field in target.columns}
    identity_spans: list[tuple[int, int]] = []
    for sentence in re.finditer(r"[^。；;!?！？]+", text):
        identity_spans.extend(
            (sentence.start() + a, sentence.start() + b)
            for a, b in profile_field_identity_spans(sentence[0], fields)
        )
    from apps.orchestrator.control.claims import _measurement_labels

    # Ownership uses observed field identities even when their source is not
    # eligible for support in the requested scope. Such a field can require
    # rejection, but cannot grant a value binding or make a new statistic role.
    owner_fields = {
        str(item["metric"])
        for record in evidence
        for item in record.summary.get("value_index", [])
        if isinstance(item, dict) and isinstance(item.get("metric"), str)
    }
    expressions = count_expressions(
        _plain(text), measurement_labels=_measurement_labels(), owner_fields=owner_fields
    )
    for expression in expressions:
        if any(
            a <= expression.value_span[0] < expression.value_span[1] <= b for a, b in identity_spans
        ):
            continue
        left, right = expression.value_span
        token = text[left:right]
        local, start, end = _local(text, left, right)
        # A soft line break belongs to a head; actual member lines end it.
        next_members = [m for m in members if m.start >= right]
        first = next_members[0] if next_members else None
        head_end = first.marker[0] if first and first.marker[0] < end else end
        head = text[start:head_end].strip()
        # Preserve offsets after trimming the source prefix.
        leading = len(text[start:head_end]) - len(text[start:head_end].lstrip())
        kind = _head_kind(head, left - start - leading, right - start - leading)
        purpose = "business" if kind in {"business", "diagnostics"} else "unknown"
        audit: dict[str, Any] = {
            "token": token,
            "source_span": [left, right],
            "quantity_purpose": purpose,
            "owner": kind,
            "binding": "unknown",
            "structure": "complete" if closed else "incomplete",
            "obligation": "blocked",
        }
        result.quantities.append(audit)
        if any(a <= left < b for a, b in hidden):
            audit["owner"] = "quoted_or_code"
            continue
        if expression.requested_output:
            audit.update(
                quantity_purpose="pending_outline", binding="request", obligation="pending"
            )
            continue  # Existing TaskContract decides requested outputs; not delivered facts.
        amount = _amount(_plain(token).strip())
        selected: list[Member] = []
        if first and not text[head_end : first.marker[0]].strip():
            base = first.indent
            previous_end = first.end
            for member in next_members:
                if member is not first and (
                    member.indent < base or text[previous_end : member.marker[0]].strip()
                ):
                    break
                if member.indent == base:
                    selected.append(member)
                previous_end = member.end
        inline_ids: list[str] | None = None
        if kind == "theme":
            # A known neutral theme does not choose document vs business by
            # comparing its number. Its complete members decide the owner.
            pure_advice = bool(selected) and all(
                inspection_member(m.text, evidence, goal=goal) is not None
                or recommendation_member(m.text, evidence, goal=goal) is not None
                for m in selected
            )
            if pure_advice:
                kind = "document"
            else:
                matching = _matching_diagnostics(head, goal, evidence)
                colon = re.search(r"[:：]", text[right:head_end])
                body = text[right + colon.end() : head_end] if colon else ""
                if len(matching) == 1:
                    inline_ids = (
                        _diagnostic_members(selected, matching[0])
                        if selected
                        else _diagnostic_issue_ids(body, matching[0])
                    )
                if inline_ids:
                    kind = "diagnostics"
                    if not selected and colon is not None:
                        # A proven heading/body relation creates a statement
                        # boundary, not a hidden region. Every heading character
                        # and value still reaches the same binder and source map.
                        view[right + colon.end() - 1] = "。"
                else:
                    kind = "document"  # Preserve its unfulfilled/unknown obligation.
            audit["owner"] = kind
            if kind == "diagnostics":
                audit["quantity_purpose"] = "business"
        if kind != "business" and (kind == "labels" or expression.referenced_items is not None):
            label_members = _label_members(text, left, right)
            if label_members and not expression.qualified:
                # A label enumeration proves its own membership only. Embedded
                # affirmative numbers remain in numeric_text for the fact binder.
                actual = len(label_members)
                proof_members = label_members
                if amount == actual:
                    _document_proof(
                        result, audit, view, left, right, proof_members, "quoted_labels"
                    )
                continue
        if kind == "diagnostics":
            matching = _matching_diagnostics(head, goal, evidence)
            if len(matching) == 1 and amount is not None and not expression.qualified:
                e = matching[0]
                count = next(
                    i["value"] for i in e.summary["value_index"] if i["path"] == QUALITY_COUNT_PATH
                )
                supported = str(amount) == count
                audit.update(
                    binding="supported" if supported else "unsupported", obligation="fulfilled"
                )
                result.refs.append(
                    {
                        "token": token,
                        "supported": supported,
                        "quantity_purpose": "business",
                        "evidence_id": e.evidence_id,
                        "path": QUALITY_COUNT_PATH,
                        "evidence_value": count,
                        "semantic_binding": _quality_binding(e),
                        "statement_span": [left, right],
                    }
                )
                view[left:right] = " " * (right - left)
            if inline_ids is not None or re.search(r"以下|下列|如下|[:：]\s*$", head):
                if selected:
                    owned_lists.append((start, selected[-1].end))
                ids = inline_ids or (
                    _diagnostic_members(selected, matching[0]) if len(matching) == 1 else None
                )
                if ids is None or len(ids) != amount:
                    audit["obligation"] = "pending"
                    result.issues.append("unfulfilled_diagnostic_members")
            continue
        if kind != "document":
            continue
        audit["quantity_purpose"] = "pending_outline" if not selected else "unknown"
        if not selected:
            result.issues.append("pending_outline")
            continue
        owned_lists.append((start, selected[-1].end))
        approved = [
            inspection_member(m.text, evidence, goal=goal) is not None
            or recommendation_member(m.text, evidence, goal=goal) is not None
            or (fact_validator is not None and fact_validator(m.text))
            for m in selected
        ]
        if all(approved) and amount == len(selected) and not expression.qualified:
            _document_proof(
                result,
                audit,
                view,
                left,
                right,
                [[m.start, m.end] for m in selected],
                "direct_members",
            )
        else:
            audit["binding"] = "unsupported"
    # A document-count proof ends with its own members. A subsequent deictic
    # subject creates a new unresolved ownership edge, regardless of its verb
    # or whether it contains digits. It cannot inherit the list's proof.
    # This is a bounded anaphora boundary, not a blacklist of confirmation words.
    for continuation in re.finditer(
        r"(?:^|[。！？!?\n])\s*(?:它们|他们|她们|这些|那些|上述|以上|所有|全部|"
        r"上面(?:提到的)?|前述|前面(?:提到的)?)",
        text,
    ):
        position = continuation.end()
        if any(end < position for _, end in owned_lists) and not any(
            start <= position <= end for start, end in owned_lists
        ):
            result.issues.append("unbound_list_continuation")
    result.numeric_text = "".join(view)
    return result


def _document_proof(
    result: SurfaceResult,
    audit: dict[str, Any],
    view: list[str],
    left: int,
    right: int,
    members: list[list[int]],
    basis: str,
) -> None:
    proof = {
        "schema": SURFACE_SCHEMA,
        "source_hash": result.source_hash,
        "node_id": f"quantity:{left}:{right}",
        "members": members,
        "basis": basis,
    }
    audit.update(
        quantity_purpose="document",
        binding="supported",
        obligation="fulfilled",
        document_count_proof=proof,
    )
    result.refs.append(
        {
            "token": result.source_text[left:right],
            "supported": True,
            "quantity_purpose": "document",
            "statement_span": [left, right],
            "document_count_proof": proof,
        }
    )
    view[left:right] = " " * (right - left)
