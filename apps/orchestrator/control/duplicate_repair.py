"""Run-local, auditable disposition of unsupported duplicate-count assertions."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from packages.orchestration.contracts import TaskContract
from packages.session.models import JsonObject
from packages.session.task_models import ClaimDraft, EvidenceRecord, TaskEvent

from apps.orchestrator.control.claim_structure import delimited_spans
from apps.orchestrator.control.dataset_identity import (
    IDENTITY_DECLARATION,
    identity_declarations,
    identity_literal,
    identity_matches_binding,
)
from apps.orchestrator.control.profile_claims import _clause_boundaries, _fields_in, profile_fact
from apps.orchestrator.control.verifier import VerificationIssue, VerificationResult


def restored_duplicate_obligations(events: list[TaskEvent]) -> list[JsonObject]:
    for event in reversed(events):
        value = event.payload.get("duplicate_obligations")
        if isinstance(value, list):
            return copy.deepcopy(value)
    return []


def _explicit_non_requirement(
    clause: str, fields: set[str], *, temporal_relation: str | None = None
) -> bool:
    """只在完整否定、暂缓或未来背景中免除对象，不删除顺序交付前项。"""
    names = "|".join(re.escape(field) for field in sorted(fields, key=len, reverse=True))
    subject = rf"(?:(?:{names}|整行|全行)(?:的)?)?" if names else r"(?:(?:整行|全行)(?:的)?)?"
    fact = subject + r"(?:完全)?(?:重复|去重)(?:多余记录数|记录数|计数|统计|记录|情况|数|行)?"
    facts = fact + rf"(?:\s*(?:和|及|以及|、)\s*{fact})*"
    action = r"(?:统计|计算|给出|给|提供|核验|分析|处理|输出|报告|检查|列出|展示)"
    process = r"(?:统计|计算|核验|分析|处理|检查)"
    modifier = r"(?:(?:本轮|目前|当前|现在|暂时|本次|暂且)\s*)?"
    politeness = r"(?:请)?\s*"
    negation = r"(?:不需要|无需|不必|不用|不要求|暂不|不要|可以不)"
    future = r"(?:以后|后续|下轮|下一轮|下一步)"
    exclusion = modifier + negation + rf"\s*{modifier}(?:{action}\s*)?"
    defer_prefix = modifier + r"(?:(?:可|可以)\s*)?(?:(?:留待|留到|放到)\s*)?" + future
    defer = defer_prefix + rf"\s*(?:再)?\s*{action}"
    patterns = [politeness + exclusion + facts, politeness + facts + r"\s*" + exclusion]
    if temporal_relation is None:
        patterns.extend([politeness + defer + r"\s*" + facts, politeness + facts + r"\s*" + defer])
    else:
        # In a sequence, future words alone do not waive an ordered delivery.
        # A complete postponement or explicitly conditional future clause does.
        postpone = modifier + r"(?:(?:可|可以)\s*)?(?:留待|留到|放到)\s*" + future
        postpone += rf"\s*(?:再)?\s*{action}"
        patterns.extend(
            [
                politeness + postpone + r"\s*" + facts,
                politeness + facts + r"\s*" + postpone,
                r"(?:如果|若|等|待)\s*" + defer + r"\s*" + facts,
                r"(?:如果|若|等|待)\s*" + facts + r"\s*" + defer,
            ]
        )
        if temporal_relation in {"之前", "以前"}:
            # The established 'before handling A, first deliver B' contract
            # supplies future context for handling, never for delivering A.
            process_later = defer_prefix + rf"\s*(?:再)?\s*{process}"
            patterns.extend(
                [
                    politeness + process_later + r"\s*" + facts,
                    politeness + facts + r"\s*" + process_later,
                    politeness + r"处理\s*" + facts,
                    politeness + facts + r"\s*处理",
                ]
            )
    return any(re.fullmatch(pattern, clause.strip()) for pattern in patterns)


def _current_requirement_clauses(request: str, fields: set[str]) -> list[str]:
    """先辨认有主句的时间从句，再提取本次交付对象。"""
    current = []
    for sentence in re.split(r"[。；;\n]", request):
        clauses = re.split(r"[，,]|但是|但|而且|并且", sentence)
        for clause in clauses:
            temporal = re.fullmatch(
                r"\s*(?:在)?(?P<context>.+?)(?P<relation>之前|以前|之后|以后)\s*", clause
            )
            # Only an explicitly deferred operand in a temporal subordinate
            # clause is excluded. The main clause (even for the same fact) is
            # still checked; an unexplained time word never waives delivery.
            if (
                temporal
                and len([part for part in clauses if part.strip()]) > 1
                and _explicit_non_requirement(
                    temporal["context"], fields, temporal_relation=temporal["relation"]
                )
            ):
                continue
            current.append(clause)
    return current


def _required(contract: TaskContract, subject: str, fields: set[str]) -> bool:
    requests = [contract.goal] + [c.description for c in contract.success_criteria if c.required]
    for request in requests:
        for clause in _current_requirement_clauses(request, fields):
            if "重复" not in clause and "去重" not in clause:
                continue
            named = {field for field in fields if field in clause}
            named = {
                field
                for field in named
                if not any(field != other and field in other for other in named)
            }
            whole = bool(re.search(r"整行|全行|完全重复|重复行", clause))
            matches = (
                (whole or not named)
                if subject == "整行"
                else (subject in named or (not named and not whole))
            )
            if matches and not _explicit_non_requirement(clause, fields):
                return True
    return False


def _scope_text(statement: str) -> str:
    """Unresolved textual subset restrictions cannot disappear on the next turn."""
    match = re.search(r"^(.+?(?:记录中|数据中|子集中|范围内|筛选后|过滤后))[,，：:]?", statement)
    if match:
        return match[1].strip()
    whole = re.search(r"^(.*?)整行重复", statement)
    if whole:
        prefix = whole[1].strip()
        for declaration in reversed(identity_declarations(prefix, headers_only=True)):
            if identity_literal(declaration["value"]):
                prefix = prefix[: declaration.start()] + prefix[declaration.end() :]
        prefix = prefix.strip("，,；;：: ")
        if prefix not in {
            "",
            "全表",
            "整表",
            "全数据集",
            "整个数据集",
            "当前数据集",
            "本数据集",
            "该数据集",
        }:
            return prefix
    return ""


def _targets(evidence: list[EvidenceRecord]) -> tuple[set[str], list[JsonObject]]:
    fields: set[str] = set()
    targets: list[JsonObject] = []
    for record in evidence:
        if record.kind != "tool_result" or record.source.get("tool") != "get_data_profile":
            continue
        context = record.summary.get("numeric_context") or {}
        for item in record.summary.get("value_index", []):
            fact = profile_fact(item)
            if fact is None:
                continue
            if fact["object"]["kind"] == "profile_column":
                fields.add(str(fact["metric"]))
            if fact["statistic"] not in {"duplicate_rows", "duplicate_excess_count"}:
                continue
            target = {
                "dataset_ref": context.get("dataset_ref"),
                "dataset_version": context.get("data_version_hash", context.get("dataset_ref")),
                "filters": context.get("filters", []),
                "statistic": fact["statistic"],
                "metric": fact["metric"],
                "count_basis": fact["count_basis"],
            }
            if target not in targets:
                targets.append(target)
    return fields, targets


def _unresolved_assertions(
    claims: list[ClaimDraft],
    evidence: list[EvidenceRecord],
    fields: set[str],
) -> list[tuple[ClaimDraft, str, str]]:
    """Locate each failed count in the original claim, never reparse without scope."""
    assertions: list[tuple[ClaimDraft, str, str]] = []
    for claim in claims:
        if claim.claim_kind != "numeric" or "重复" not in claim.statement:
            continue
        boundaries = _clause_boundaries(claim.statement, fields)
        for ref in claim.value_refs:
            if ref.get("supported") is True or ref.get("reason") == "unverified_dataset_identity":
                continue
            span = ref.get("statement_span")
            if isinstance(span, list) and len(span) == 2 and all(type(i) is int for i in span):
                start, end = span
                left = max((i + 1 for i in boundaries if i < start), default=0)
                right = min((i for i in boundaries if i >= end), default=len(claim.statement))
                fragment = claim.statement[left:right].strip()
                if "重复" not in fragment:
                    continue
                scope = _scope_text(claim.statement[:left]) or _scope_text(fragment)
                assertion = ClaimDraft(
                    statement=fragment,
                    claim_kind="numeric",
                    value_refs=(ref,),
                    evidence_ids=claim.evidence_ids,
                )
                assertions.append((assertion, claim.statement, scope))
            else:
                # Historical callers without positions retain conservative behavior.
                assertions.append((claim, claim.statement, _scope_text(claim.statement)))
    return assertions


def capture_duplicate_obligations(
    previous: list[JsonObject],
    claims: list[ClaimDraft],
    evidence: list[EvidenceRecord],
    contract: TaskContract,
) -> list[JsonObject]:
    obligations = copy.deepcopy(previous)
    fields, targets = _targets(evidence)
    if not targets:
        return obligations
    for claim, original, scope in _unresolved_assertions(claims, evidence, fields):
        whole = bool(re.search(r"整行|完全重复.*(?:行|记录)|重复行", claim.statement))
        named = _fields_in(claim.statement, fields)
        subject = "整行" if whole else next(iter(named)) if len(named) == 1 else "未解析重复结论"
        role = "duplicate_rows" if whole else "duplicate_excess_count"
        if "参与重复" in claim.statement:
            role = "duplicate_participants"
        elif "重复组" in claim.statement:
            role = "duplicate_groups"
        candidates = [
            t for t in targets if t["statistic"] == role and (whole or t["metric"] == subject)
        ]
        declarations = [m for m in identity_declarations(original) if identity_literal(m["value"])]
        if declarations:
            candidates = [
                t for t in candidates if all(identity_matches_binding(m, t) for m in declarations)
            ]
        target = candidates[0] if len(candidates) == 1 else None
        key = {
            "subject": subject,
            "target": target,
            "role": role,
            "scope_text": scope,
        }
        ident = hashlib.sha256(
            json.dumps(key, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if any(item["id"] == ident for item in obligations):
            continue
        obligations.append(
            {
                "id": ident,
                **key,
                "required": _required(contract, subject, fields),
                "original_statement": original,
                "assertion": claim.statement,
                "original_statement_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "status": "pending",
            }
        )
    return obligations


def _withdrawal_statements(text: str, subjects: set[str]) -> list[tuple[str, str]]:
    """Classify speech by its enclosing scope, not by a present-tense verb."""
    prose: list[str] = []
    fence: tuple[str, int, int] | None = None
    lazy_quote: int | None = None
    structural_scopes: list[tuple[int, int]] = []
    offset = 0
    for line in text.splitlines():
        mark = re.match(r"^\s*(`{3,}|~{3,})", line)
        if mark:
            if fence is None:
                fence = (mark[1][0], len(mark[1]), offset)
            elif (
                mark[1][0] == fence[0]
                and len(mark[1]) >= fence[1]
                and not line[mark.end() :].strip()
            ):
                structural_scopes.append((fence[2], offset + len(line)))
                fence = None
            prose.append(" " * len(line))
        elif fence:
            prose.append(" " * len(line))
        elif re.match(r"^\s*(?:[-*]\s+)?\s*>", line):
            if lazy_quote is None:
                lazy_quote = offset
            prose.append(" " * len(line))
        elif lazy_quote is not None and line.strip():
            prose.append(" " * len(line))
        elif not line.strip():
            if lazy_quote is not None:
                structural_scopes.append((lazy_quote, offset))
            lazy_quote = None
            prose.append(line)
        elif line.startswith(("    ", "\t")):
            prose.append(" " * len(line))
        else:
            prose.append(line)
        offset += len(line) + 1
    source = "\n".join(prose)
    if fence is not None:
        structural_scopes.append((fence[2], len(source)))
    if lazy_quote is not None:
        structural_scopes.append((lazy_quote, len(source)))
    view = list(source)
    code_start: tuple[int, int, int] | None = None
    for mark in re.finditer(r"`+", source):
        if code_start is None:
            code_start = (mark.start(), mark.end(), len(mark[0]))
        elif len(mark[0]) == code_start[2]:
            left, body, _ = code_start
            if source[body : mark.start()] in subjects:
                view[left:body] = " " * (body - left)
                view[mark.start() : mark.end()] = " " * len(mark[0])
            else:
                view[left : mark.end()] = " " * (mark.end() - left)
                structural_scopes.append((left, mark.end()))
            code_start = None
    if code_start is not None:
        view[code_start[0] :] = " " * (len(source) - code_start[0])
    for left, right in delimited_spans(source):
        if source[left] not in "\"'`“‘「『":
            continue
        if source[left + 1 : right - 1] in subjects:
            view[left] = view[right - 1] = " "
        else:
            view[left:right] = " " * (right - left)
            structural_scopes.append((left, right))
    out: list[tuple[str, str]] = []
    hypothetical: list[int | None] = []
    previous_end = 0
    paragraph_start = 0
    for match in re.finditer(r"[^。；;\n]+", "".join(view)):
        statement = match[0].strip().lstrip("-* ")
        if not statement:
            continue
        gap = source[previous_end : match.start()]
        enclosing = [end for end in hypothetical if end is None or end > match.start()]
        if hypothetical and not enclosing:
            paragraph_start = len(out)
        hypothetical = enclosing
        if "\n\n" in gap and not hypothetical:
            paragraph_start = len(out)
        previous_end = match.end()
        # Postposed scope belongs to the preceding speech, not a new action.
        retrospective = re.match(
            r"(?:以上|上述)(?:的)?(?:仅|只)?(?:为|是)?(?:一段)?(?:示例|模板|引用)",
            statement,
        )
        if retrospective:
            del out[paragraph_start:]
            hypothetical.append(None)
            continue
        introduction = bool(
            re.match(r"(?:若|如果|假如|假设|假定|假使|一旦|例如|比如|举例)", statement)
            or re.match(
                r"(?:以下|下面|这段|这|此)?(?:只|仅)?(?:是|为)?"
                r"(?:(?:尚未|未)(?:采用|执行)的)?(?:一个)?(?:示例|模板|引用|原文)",
                statement,
            )
            or re.search(r"(?:仅为|只是)(?:示例|模板)|(?:并非|不是)(?:本次|实际)撤回", statement)
        )
        if introduction:
            visible_end = match.start() + len(match[0].rstrip())
            introduced = [
                (left, right)
                for left, right in structural_scopes
                if left >= visible_end and not text[visible_end:left].strip(" \t\r\n:：")
            ]
            # The introduction owns only its immediately attached quotation.
            # Closing a later child/title cannot release an unbounded outer
            # conditional/template; each enclosing boundary remains active.
            hypothetical.append(min(introduced)[1] if introduced else None)
            continue
        # Only this introduction's matched structural boundary releases it.
        if not hypothetical:
            out.append((source[match.start() : match.end()].strip(), statement))
    return out


def _explicit_withdrawal(text: str, item: JsonObject, obligations: list[JsonObject]) -> str | None:
    subject = str(item["subject"])
    subjects = {str(other["subject"]) for other in obligations}
    selection = r"(?P<all>全部|所有)?\s*(?P<subject>" + re.escape(subject) + r")?"
    label = selection + r"\s*(?:的)?\s*重复(?:计数|统计)?(?:结论|说法)"
    verb = r"(?:我)?(?:现|现在)?(?:明确)?撤回(?:此前|先前|上述)?(?:未经核实的)?\s*"
    result: str | None = None
    header_context: list[re.Match[str]] = []
    header_statements: list[str] = []
    header_used = False
    for original, statement in _withdrawal_statements(text, subjects):
        declarations = []
        remainder = statement
        while declaration := IDENTITY_DECLARATION.match(remainder):
            declarations.append(declaration)
            remainder = remainder[declaration.end() :].lstrip(" ，,：:")
        if declarations:
            if remainder or header_used:
                header_context = []
                header_statements = []
                header_used = False
            if remainder:
                header_context = declarations
                header_statements = []
            else:
                header_context.extend(declarations)
                header_statements.append(original)
                continue
        declarations = header_context
        header_used = True
        target = item.get("target") or {}
        if declarations and not all(identity_matches_binding(m, target) for m in declarations):
            continue
        # An explicit denial of this disposition is not overridden by an earlier
        # positive sentence for the same fact.
        if re.match(
            r"(?:我)?(?:(?:现在|现)\s*)?(?:尚未|并未|没有|未曾|并不|不)(?:实际)?撤回(?:上述结论|"
            + label
            + r")",
            remainder,
        ):
            result = None
            continue
        match = re.match(verb + label + r"(?=$|[，,：:])", remainder)
        if match is None or not (match["all"] or match["subject"]):
            continue
        candidates = [
            other
            for other in obligations
            if (not match["subject"] or other["subject"] == subject)
            and all(identity_matches_binding(m, other.get("target") or {}) for m in declarations)
        ]
        if match["all"] or (len(candidates) == 1 and candidates[0]["id"] == item["id"]):
            result = "\n".join([*header_statements, original])
    return result


def resolve_duplicate_obligations(
    obligations: list[JsonObject],
    claims: list[ClaimDraft],
    final_text: str,
) -> list[JsonObject]:
    updated = copy.deepcopy(obligations)
    for item in updated:
        target = item.get("target")
        resolved = []
        if isinstance(target, dict):
            for claim in claims:
                if _scope_text(claim.statement) != item.get("scope_text", ""):
                    continue
                for ref in claim.value_refs:
                    binding = ref.get("semantic_binding") or {}
                    if ref.get("supported") is True and all(
                        binding.get(k) == v for k, v in target.items()
                    ):
                        resolved.append(
                            {
                                "statement": claim.statement,
                                "binding": binding,
                                "evidence_id": ref.get("evidence_id", ref.get("artifact_id")),
                            }
                        )
        if resolved:
            item["status"] = "resolved"
            item["resolution"] = resolved
        elif withdrawal := _explicit_withdrawal(final_text, item, updated):
            item["status"] = "unfulfilled_required" if item["required"] else "withdrawn_optional"
            item["resolution"] = {
                "kind": "explicit_withdrawal",
                "statement": withdrawal,
                "statement_sha256": hashlib.sha256(withdrawal.encode()).hexdigest(),
                "satisfies_requirement": not item["required"],
            }
        else:
            item["status"] = "pending"
            item.pop("resolution", None)
    return updated


def unresolved_duplicate_obligations(obligations: list[JsonObject]) -> bool:
    return any(item.get("status") not in {"resolved", "withdrawn_optional"} for item in obligations)


def enforce_duplicate_obligations(
    verification: VerificationResult,
    obligations: list[JsonObject],
) -> VerificationResult:
    if not unresolved_duplicate_obligations(obligations):
        return verification
    issue = VerificationIssue(
        "unsupported_numeric_claim",
        "先前重复计数尚未处理：必交付内容须核验；非必需附加结论须明确撤回，不能静默删除。",
    )
    return VerificationResult(
        "NEEDS_ACTION" if verification.passed else verification.verdict,
        (*verification.issues, issue),
    )


def duplicate_obligation_instruction(obligations: list[JsonObject]) -> str:
    items = []
    for item in obligations:
        if item.get("status") in {"resolved", "withdrawn_optional"}:
            continue
        label = json.dumps(item["subject"], ensure_ascii=False)
        if item["required"]:
            items.append(
                f"对象{label}属于用户/契约要求：按对应数据集、版本与范围补证；无法完成须明确未完成，撤回不能满足要求。"
            )
        else:
            items.append(
                f"对象{label}是模型附加结论：可按已有工具事实修复，也可在引用和示例外明确写‘我现明确撤回{item['subject']}重复计数结论’，不必补算用户未要求的内容。"
            )
    return "\n".join(items)
