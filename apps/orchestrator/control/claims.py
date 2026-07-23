"""Deterministic Claim extraction and Evidence linking.

Stage 1 deliberately keeps this extractor conservative: numeric claims require
an exact value present in current-run Evidence, while knowledge claims require
an explicit source label returned by ``kb_search``.  It does not ask a model to
invent links between prose and Evidence.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from packages.session.models import JsonObject
from packages.session.task_models import ClaimDraft, EvidenceRecord

_NUMBER_PATTERN = re.compile(
    r"(?<![\w.\-])"
    r"[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?%?(?![\w.\-])"
)
_LEADING_LIST_MARKER = re.compile(r"^\s*(?:[-*]\s+|\d+[.)、]\s*)")
_MAX_EVIDENCE_VALUES = 256
_MAX_EVIDENCE_SOURCES = 64
_MAX_VALUE_DEPTH = 8
_NO_KNOWLEDGE_RESULT_PATTERN = re.compile(
    r"(?:未找到|没有找到|没查到|未检索到|没有检索到|没能检索到|无相关|没有相关|检索无结果|"
    r"无法(?:依据|从).{0,12}(?:回答|确认)|"
    r"知识库.{0,8}(?:没有|无|未收录))"
)
_LIMITATION_PATTERN = re.compile(
    r"(?:局限|限制|注意|仅供参考|不能代表|不代表因果|样本不足|缺少|不确定)"
)
_PATH_ALIASES: dict[str, tuple[str, ...]] = {
    "row_count": ("总行数", "数据行", "记录数", "样本数"),
    "column_count": ("总列数", "字段数", "列数"),
    "duplicate_rows": ("重复记录", "重复行", "重复"),
    "null_count": ("空值", "缺失值", "缺失"),
    "null_ratio": ("空值率", "缺失率"),
    "p_value": ("p值", "p 值", "显著性"),
    "n_anomalies": ("异常数", "异常点", "异常记录"),
}


def build_evidence_summary(
    *, summary: str, result: Any, artifact_id: str | None
) -> JsonObject:
    """Create bounded value/source indexes without copying the whole result."""
    values: list[JsonObject] = []
    sources: list[JsonObject] = []
    _collect_values(result, "$", values, depth=0)
    _collect_sources(result, "$", sources, depth=0)
    return {
        "summary": summary,
        "artifact_id": artifact_id,
        "value_index": values,
        "value_index_truncated": len(values) >= _MAX_EVIDENCE_VALUES,
        "source_index": sources,
        "source_index_truncated": len(sources) >= _MAX_EVIDENCE_SOURCES,
        "knowledge_empty": _knowledge_result_is_empty(result),
    }


def extract_claims(
    *, final_text: str, goal: str, evidence: list[EvidenceRecord]
) -> list[ClaimDraft]:
    """Extract every deterministic Stage-1 Claim type from a candidate answer."""
    claims = extract_numeric_claims(
        final_text=final_text,
        goal=goal,
        evidence=evidence,
    )
    claims.extend(extract_knowledge_claims(final_text=final_text, evidence=evidence))
    claims.extend(extract_limitation_claims(final_text=final_text))
    return claims


def extract_numeric_claims(
    *, final_text: str, goal: str, evidence: list[EvidenceRecord]
) -> list[ClaimDraft]:
    """Extract numeric statements and link each number to current-run Evidence."""
    goal_numbers = {
        normalized
        for token in _numeric_tokens(goal)
        if (normalized := _normalize_number(token)) is not None
    }
    evidence_values = _evidence_values(evidence)
    claims: list[ClaimDraft] = []
    for raw_statement in _split_statements(final_text):
        statement = _LEADING_LIST_MARKER.sub("", raw_statement).strip()
        if not statement:
            continue
        refs: list[JsonObject] = []
        linked_ids: list[str] = []
        for match in _NUMBER_PATTERN.finditer(statement):
            token = match.group(0)
            normalized = _normalize_number(token)
            if normalized is None or (
                normalized in goal_numbers
                and _looks_like_time_scope(statement, match.start(), match.end())
            ):
                continue
            matched, candidates = _match_evidence_value(
                normalized, evidence_values, statement=statement
            )
            if matched is None:
                refs.append(
                    {
                        "token": token,
                        "normalized": str(normalized),
                        "supported": False,
                    }
                )
                continue
            evidence_id, path, evidence_value = matched
            value_ref: JsonObject = {
                "token": token,
                "normalized": str(normalized),
                "supported": True,
                "evidence_id": evidence_id,
                "path": path,
                "evidence_value": evidence_value,
            }
            if len(candidates) > 1:
                value_ref["candidate_paths"] = [
                    {"evidence_id": item[0], "path": item[1]}
                    for item in candidates
                ]
                value_ref["ambiguous_value"] = True
            refs.append(value_ref)
            for candidate_id, _candidate_path, _candidate_value in candidates:
                if candidate_id not in linked_ids:
                    linked_ids.append(candidate_id)
        if refs:
            claims.append(
                ClaimDraft(
                    statement=statement,
                    claim_kind="numeric",
                    value_refs=tuple(refs),
                    evidence_ids=tuple(linked_ids),
                )
            )
    return claims


def extract_knowledge_claims(
    *, final_text: str, evidence: list[EvidenceRecord]
) -> list[ClaimDraft]:
    """Link knowledge prose to explicit current-run ``kb_search`` sources.

    The whole answer is one conservative knowledge Claim.  This avoids claiming
    sentence-level semantic alignment that Stage 1 cannot establish reliably,
    while still preventing an uncited or fabricated knowledge answer from being
    delivered.
    """
    knowledge = [
        record for record in evidence if record.source.get("tool") == "kb_search"
    ]
    if not knowledge or not final_text.strip():
        return []

    source_candidates: list[tuple[str, str, str, str | None]] = []
    for record in knowledge:
        raw_index = record.summary.get("source_index")
        if not isinstance(raw_index, list):
            continue
        for raw in raw_index:
            if not isinstance(raw, dict):
                continue
            source = raw.get("source")
            path = raw.get("path")
            section = raw.get("section")
            if isinstance(source, str) and source.strip() and isinstance(path, str):
                source_candidates.append(
                    (
                        record.evidence_id,
                        path,
                        source.strip(),
                        section.strip()
                        if isinstance(section, str) and section.strip()
                        else None,
                    )
                )

    refs: list[JsonObject] = []
    linked_ids: list[str] = []
    lowered = final_text.casefold()
    for evidence_id, path, source, section in source_candidates:
        if source.casefold() not in lowered:
            continue
        ref: JsonObject = {
            "kind": "knowledge_source",
            "source": source,
            "path": path,
            "supported": True,
            "evidence_id": evidence_id,
            "confidence": "explicit_source_match",
        }
        if section is not None:
            ref["section"] = section
        refs.append(ref)
        if evidence_id not in linked_ids:
            linked_ids.append(evidence_id)

    if source_candidates and not refs:
        refs.append(
            {
                "kind": "knowledge_source",
                "supported": False,
                "reason": "missing_source_citation",
                "available_sources": sorted({item[2] for item in source_candidates}),
            }
        )
    elif not source_candidates:
        honest_no_result = (
            all(record.summary.get("knowledge_empty") is True for record in knowledge)
            and _NO_KNOWLEDGE_RESULT_PATTERN.search(final_text) is not None
        )
        refs.append(
            {
                "kind": "knowledge_no_result",
                "supported": honest_no_result,
                "reason": (
                    "knowledge_search_empty"
                    if honest_no_result
                    else "affirmative_claim_without_knowledge_source"
                ),
                "evidence_id": knowledge[0].evidence_id,
                "confidence": "explicit_no_result" if honest_no_result else "unsupported",
            }
        )
        linked_ids.append(knowledge[0].evidence_id)

    return [
        ClaimDraft(
            statement=final_text.strip()[:4000],
            claim_kind="knowledge",
            value_refs=tuple(refs),
            evidence_ids=tuple(linked_ids),
        )
    ]


def extract_limitation_claims(*, final_text: str) -> list[ClaimDraft]:
    """Record explicit limitations for later semantic verification/audit."""
    claims: list[ClaimDraft] = []
    for raw_statement in _split_statements(final_text):
        statement = _LEADING_LIST_MARKER.sub("", raw_statement).strip()
        if statement and _LIMITATION_PATTERN.search(statement):
            claims.append(
                ClaimDraft(
                    statement=statement,
                    claim_kind="limitation",
                    value_refs=(
                        {
                            "kind": "claim_metadata",
                            "confidence": "explicitly_qualified",
                        },
                    ),
                    evidence_ids=(),
                )
            )
    return claims


def _collect_values(
    value: Any, path: str, output: list[JsonObject], *, depth: int
) -> None:
    if len(output) >= _MAX_EVIDENCE_VALUES or depth > _MAX_VALUE_DEPTH:
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float | Decimal):
        normalized = _decimal_from_value(value)
        if normalized is not None:
            output.append(
                {"path": path, "value": str(normalized), "source_kind": "number"}
            )
        return
    if isinstance(value, str):
        for index, token in enumerate(_numeric_tokens(value)):
            if len(output) >= _MAX_EVIDENCE_VALUES:
                return
            normalized = _normalize_number(token)
            if normalized is not None:
                output.append(
                    {
                        "path": f"{path}#number[{index}]",
                        "value": str(normalized),
                        "source_kind": "text",
                    }
                )
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_values(child, f"{path}.{key}", output, depth=depth + 1)
            if len(output) >= _MAX_EVIDENCE_VALUES:
                return
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_values(child, f"{path}[{index}]", output, depth=depth + 1)
            if len(output) >= _MAX_EVIDENCE_VALUES:
                return


def _collect_sources(
    value: Any, path: str, output: list[JsonObject], *, depth: int
) -> None:
    if len(output) >= _MAX_EVIDENCE_SOURCES or depth > _MAX_VALUE_DEPTH:
        return
    if isinstance(value, dict):
        source = value.get("source")
        if isinstance(source, str) and source.strip() and isinstance(value.get("text"), str):
            item: JsonObject = {"path": path, "source": source.strip()}
            section = value.get("section")
            if isinstance(section, str) and section.strip():
                item["section"] = section.strip()
            output.append(item)
        for key, child in value.items():
            _collect_sources(child, f"{path}.{key}", output, depth=depth + 1)
            if len(output) >= _MAX_EVIDENCE_SOURCES:
                return
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_sources(child, f"{path}[{index}]", output, depth=depth + 1)
            if len(output) >= _MAX_EVIDENCE_SOURCES:
                return


def _knowledge_result_is_empty(result: Any) -> bool | None:
    if not isinstance(result, dict) or not isinstance(result.get("is_empty"), bool):
        return None
    return bool(result["is_empty"])


def _evidence_values(
    evidence: list[EvidenceRecord],
) -> list[tuple[str, str, Decimal, str]]:
    indexed: list[tuple[str, str, Decimal, str]] = []
    for record in evidence:
        raw_index = record.summary.get("value_index")
        if not isinstance(raw_index, list):
            continue
        for raw in raw_index:
            if not isinstance(raw, dict):
                continue
            path = raw.get("path")
            value = raw.get("value")
            if not isinstance(path, str) or not isinstance(value, str):
                continue
            normalized = _normalize_number(value)
            if normalized is not None:
                indexed.append((record.evidence_id, path, normalized, value))
    return indexed


def _match_evidence_value(
    claim_value: Decimal,
    values: list[tuple[str, str, Decimal, str]],
    *,
    statement: str,
) -> tuple[tuple[str, str, str] | None, list[tuple[str, str, str]]]:
    candidates = [
        (evidence_id, path, raw_value)
        for evidence_id, path, evidence_value, raw_value in values
        if evidence_value == claim_value
    ]
    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], candidates

    scored = [(_path_relevance(item[1], statement), item) for item in candidates]
    highest = max(score for score, _item in scored)
    best = [item for score, item in scored if score == highest]
    # Prefer a unique semantic path match. Otherwise retain every candidate in
    # the Claim instead of silently pretending the first path is unambiguous.
    chosen = best[0] if highest > 0 and len(best) == 1 else candidates[0]
    return chosen, candidates


def _path_relevance(path: str, statement: str) -> int:
    lowered = statement.casefold().replace("_", "")
    parts = [part for part in re.split(r"[.\[\]#]", path) if part]
    leaf = parts[-1].casefold() if parts else ""
    score = 0
    if leaf and leaf.replace("_", "") in lowered:
        score += 3
    for field, aliases in _PATH_ALIASES.items():
        if field in path and any(alias.casefold() in lowered for alias in aliases):
            score += 2
    return score


def _looks_like_time_scope(statement: str, start: int, end: int) -> bool:
    """Exclude explicit time-scope restatements, not arbitrary goal-equal values."""
    before = statement[max(0, start - 8) : start]
    after = statement[end : end + 8]
    if re.match(r"\s*(?:年|月|日|季度|季|周|时|分|秒)", after):
        return True
    return bool(
        re.search(r"(?:按|在|从|至|截至|期间|范围)\s*$", before)
        and re.match(r"\s*(?:年度|月份|日期|季度|周期)", after)
    )


def _split_statements(text: str) -> list[str]:
    # findall with the deliberately simple boundary expression avoids an LLM
    # sentence splitter while keeping the original statement for audit display.
    statements: list[str] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n]+", text):
        end = match.end()
        statements.append(text[start:end])
        start = end
    if start < len(text):
        statements.append(text[start:])
    return statements


def _numeric_tokens(text: str) -> list[str]:
    return [match.group(0) for match in _NUMBER_PATTERN.finditer(text)]


def _normalize_number(token: str) -> Decimal | None:
    clean = token.rstrip("%").replace(",", "")
    try:
        value = Decimal(clean)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _decimal_from_value(value: int | float | Decimal) -> Decimal | None:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        normalized = Decimal(str(value))
    except InvalidOperation:
        return None
    return normalized if normalized.is_finite() else None
