"""Fail-closed numeric validation of generated report prose before publication.

Only selected, persisted data artifacts supply numbers. Model arguments, old
interpretations and generated reports must never become their own evidence.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from packages.common.exact_numbers import decode_exact_number
from packages.common.report_safety import escape_report_markup, sanitize_report_html
from packages.session.models import Artifact, JsonObject
from packages.session.task_models import ClaimDraft, EvidenceRecord

from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims


class _VisibleText(HTMLParser):
    def __init__(self, quote_fence: str, inline_sources: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.header: list[str] = []
        self.header_row = False
        self.in_table = False
        self.quoted_depth = 0
        self.quote_fence = quote_fence
        self.inline_sources = inline_sources

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Reconstruct structural boundaries for the same bounded heading binder
        # used by chat. Rendered quotations/code must not become author headings.
        if tag in {"pre", "blockquote"}:
            if self.quoted_depth == 0:
                self.parts.append("\n\n" + self.quote_fence + "\n")
            self.quoted_depth += 1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "strong":
            (self.cell if self.cell is not None else self.parts).append("**")
        elif tag == "p":
            self.parts.append("\n\n")
        if tag == "table":
            self.header = []
            self.in_table = True
        elif tag == "tr":
            self.row = []
            self.header_row = False
        elif tag in {"td", "th"}:
            self.cell = []
            self.header_row = self.header_row or tag == "th"

    def handle_data(self, data: str) -> None:
        data = self.inline_sources.get(data, data)
        if self.in_table and self.cell is None and not data.strip():
            return
        (self.cell if self.cell is not None else self.parts).append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"pre", "blockquote"}:
            self.quoted_depth -= 1
            if self.quoted_depth == 0:
                self.parts.append("\n" + self.quote_fence + "\n\n")
        elif tag == "strong":
            (self.cell if self.cell is not None else self.parts).append("**")
        if tag == "table":
            self.in_table = False
        elif tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.header_row:
                self.header = self.row
                self.parts.extend(
                    [
                        "\n| " + " | ".join(self.row) + " |\n",
                        "| " + " | ".join("---" for _ in self.row) + " |\n",
                    ]
                )
            else:
                self.parts.append("| " + " | ".join(self.row) + " |\n")
            self.row = None
        elif tag in {"p", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n")


def _visible_text(text: str, *, heading: bool = False) -> str:
    # Lazy: core API startup must not require the optional report dependencies.
    markdown = importlib.import_module("markdown")
    source = escape_report_markup(text)
    if heading:
        source = "### " + source  # Same heading context used by the report formatter.
    converter = markdown.Markdown(extensions=["tables", "fenced_code"])
    inline_sources: dict[str, str] = {}
    processor = converter.inlinePatterns["backtick"]
    original_match = processor.handleMatch

    def source_match(match: re.Match[str], data: str) -> Any:
        element, left, right = original_match(match, data)
        if not isinstance(element, str):
            # Capture the actual Markdown processor's matched source, including
            # delimiter width and internal backticks. Do not invent quotations
            # from rendered code, or exempt code content from numeric checking.
            token = "CHATBI_INLINE_SOURCE_" + str(len(inline_sources))
            while token in source or token in inline_sources:
                token += "_"
            inline_sources[token] = unescape(data[left:right])
            element.text = token
        return element, left, right

    processor.handleMatch = source_match
    rendered = converter.convert(source)
    visible_html = sanitize_report_html(rendered)
    # Text inside quotations/code cannot forge our structural closing marker,
    # even when backticks were written as HTML character references.
    longest = max((len(m[0]) for m in re.finditer(r"`+", unescape(visible_html))), default=0)
    parser = _VisibleText("`" * max(3, longest + 1), inline_sources)
    parser.feed(visible_html)
    parser.close()
    # Preserve Unicode value/identifier spelling. Numeric tokenization and
    # percentage scale are shared with chat; whole-text NFKC or removal of
    # format characters could silently change a value or merge field names.
    visible = "".join(parser.parts)
    # Real Markdown list counters are absent from HTML text already. A numeric
    # heading such as "123. 收入" is data, not an enumeration marker to discard.
    # Do not inject a marker between lines of one statistical assertion.
    # The extraction call keeps literal numeric heading counters instead.
    return visible


def _numeric_data(value: Any) -> Any:
    """Only typed tool numbers; free text (including JSON-looking text) is not proof."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict):
        if decode_exact_number(value) is not None:
            return value
        return {key: _numeric_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numeric_data(item) for item in value]
    return None


def validate_report_narratives(
    artifacts: list[Artifact],
    args: JsonObject,
    *,
    repair_obligations: list[JsonObject] | None = None,
) -> JsonObject:
    dataset_refs = {artifact.dataset_ref for artifact in artifacts if artifact.dataset_ref}
    if len(dataset_refs) > 1:
        raise ValueError("报告分析来自不同数据集版本/总体，请选择同一派生数据集的分析")
    evidence: list[EvidenceRecord] = []
    sections = [
        ("title", args["title"], True),
        ("insights", args.get("insights", ""), False),
    ]
    source_hashes: dict[str, str] = {}
    for artifact in artifacts:
        payload = artifact.payload or {}
        if artifact.type == "profile":
            quality = payload.get("quality") or {}
            # Only deterministic quality measurements, never recommendations,
            # role labels or model prose. Keep measurements before large samples
            # so the bounded evidence index cannot drop the quality result.
            data = {
                "quality": {
                    "duplicate_rows": quality.get("duplicate_rows"),
                    "high_null_columns": quality.get("high_null_columns"),
                    "summary": {
                        key: (quality.get("summary") or {}).get(key)
                        for key in ("issue_count", "high", "medium", "low")
                    },
                    "issues": [
                        {
                            "code": issue.get("code"),
                            "columns": issue.get("columns", []),
                            "evidence": issue.get("evidence"),
                        }
                        for issue in quality.get("issues", [])
                        if isinstance(issue, dict)
                    ],
                },
                # Exact counts only, not role confidence, suggestions or prose.
                # The shared profile binder checks these against named columns'
                # exact null_count and row_count before admitting a fact.
                "roles": {
                    "columns": [
                        {
                            "column": role.get("column"),
                            "profile_evidence": {
                                "non_null_count": (role.get("profile_evidence") or {}).get(
                                    "non_null_count"
                                )
                            },
                        }
                        for role in (payload.get("roles") or {}).get("columns", [])
                        if isinstance(role, dict)
                    ]
                },
                "profile": payload.get("profile", payload),
            }
        elif artifact.type == "table" and artifact.source_tool == "aggregate_preview":
            # Match the table types supported by report assembly. Index the
            # tool's full-data total before bounded display rows; do not derive
            # a replacement total from a possibly truncated preview.
            data = {
                "total_value": payload.get("total_value"),
                "group_total": payload.get("group_total"),
                "rows": [
                    {
                        key: row.get(key)
                        for key in (
                            "group",
                            "value",
                            "count",
                            "row_count",
                            "valid_value_count",
                        )
                    }
                    for row in payload.get("rows", [])
                    if isinstance(row, dict)
                ],
            }
        elif artifact.type == "stats":
            data = payload.get("result", payload)
        elif artifact.type == "chart":
            # ECharts style/layout numbers and model-authored labels are not data.
            option = payload.get("option") or {}
            data = {"option": option}
            titles = option.get("title") or []
            if isinstance(titles, dict):
                titles = [titles]
            for index, title in enumerate(titles):
                if isinstance(title, dict):
                    for key in ("text", "subtext"):
                        if title.get(key):
                            sections.append(
                                (
                                    f"{artifact.id}.title[{index}].{key}",
                                    str(title[key]),
                                    True,
                                )
                            )
        else:
            continue
        if artifact.type in {"chart", "stats"}:
            for name in ("caption", "interpretation"):
                if payload.get(name):
                    sections.append(
                        (f"{artifact.id}.{name}", str(payload[name]), name == "caption")
                    )
        # Bare legacy stats payloads may mix results with prose. Only typed
        # numbers survive; none of the captions/interpretations enter the index.
        numeric_data = _numeric_data(data)
        numeric_summary = build_evidence_summary(
            summary="",
            result=data,
            artifact_id=artifact.id,
            arguments={**(artifact.params or {}), "dataset_ref": artifact.dataset_ref},
        )
        result_hash = hashlib.sha256(
            json.dumps(
                {
                    "numbers": numeric_data,
                    "context": numeric_summary["numeric_context"],
                    "analysis": numeric_summary["analysis_context"],
                    "bindings": [
                        item
                        for item in numeric_summary["value_index"]
                        if item.get("source_kind") == "number"
                    ],
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        source_hashes[artifact.id] = result_hash
        evidence.append(
            EvidenceRecord(
                evidence_id=artifact.id,
                run_id="report-validation",
                invocation_id="",
                artifact_id=artifact.id,
                kind=artifact.type,
                source={"artifact_id": artifact.id, "tool": artifact.source_tool},
                result_hash=result_hash,
                created_at=artifact.created_at,
                summary=numeric_summary,
            )
        )

    verified: list[JsonObject] = []
    delivered_claims: list[ClaimDraft] = []
    delivered_text: list[str] = []
    for name, text, heading in sections:
        if not text:
            continue
        visible = _visible_text(text, heading=heading)
        delivered_text.append(visible)
        for claim in extract_numeric_claims(
            final_text=visible,
            goal=_visible_text(str(args["title"])),
            evidence=evidence,
            evidence_is_typed=True,
            strip_list_markers=False,
        ):
            unsupported = [ref["token"] for ref in claim.value_refs if not ref.get("supported")]
            if unsupported:
                raise ValueError(
                    f"报告正文数值缺少所选分析的证据（{name}: {', '.join(unsupported[:5])}）；"
                    "请使用工具计算并选择对应分析，或删除无法核实的数字后重试"
                )
            delivered_claims.append(claim)
            verified.append(
                {
                    "section": name,
                    "value_refs": [
                        {
                            **{key: value for key, value in ref.items() if key != "evidence_id"},
                            "artifact_id": ref.get("evidence_id"),
                        }
                        for ref in claim.value_refs
                    ],
                }
            )
    if repair_obligations:
        from apps.orchestrator.control.duplicate_repair import (
            resolve_duplicate_obligations,
            unresolved_duplicate_obligations,
        )

        disposition = resolve_duplicate_obligations(
            repair_obligations, delivered_claims, "\n".join(delivered_text)
        )
        if unresolved_duplicate_obligations(disposition):
            raise ValueError(
                "报告仍有未解决的重复计数：必交付内容须核验，非必需结论须明确撤回；不能静默删文。"
            )
    return {
        "status": "passed",
        "validator": "selected-artifact-numeric-v1",
        "semantic_contract": "typed-measurement-binding-v1",
        "source_hashes": source_hashes,
        "claims": verified,
    }
