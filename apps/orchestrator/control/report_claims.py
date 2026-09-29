"""Fail-closed numeric validation of generated report prose before publication.

Only selected, persisted data artifacts supply numbers. Model arguments, old
interpretations and generated reports must never become their own evidence.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import unicodedata
from html.parser import HTMLParser
from typing import Any

from packages.common.report_safety import escape_report_markup, sanitize_report_html
from packages.session.models import Artifact, JsonObject
from packages.session.task_models import EvidenceRecord

from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li", "pre", "blockquote", "td", "th", "h1", "h2", "h3"}:
            self.parts.append("\n")


def _visible_text(text: str, *, heading: bool = False) -> str:
    # Lazy: core API startup must not require the optional report dependencies.
    markdown = importlib.import_module("markdown")
    parser = _VisibleText()
    source = escape_report_markup(text)
    if heading:
        source = "### " + source  # Same heading context used by the report formatter.
    rendered = markdown.markdown(source, extensions=["tables", "fenced_code"])
    parser.feed(sanitize_report_html(rendered))
    parser.close()
    visible = "".join(
        char
        for char in unicodedata.normalize("NFKC", "".join(parser.parts))
        if unicodedata.category(char) != "Cf"
    )
    # Real Markdown list counters are absent from HTML text already. A numeric
    # heading such as "123. 收入" is data, not an enumeration marker to discard.
    return "\n".join(f"报告内容：{line}" for line in visible.splitlines())


def _numeric_data(value: Any) -> Any:
    """Only typed tool numbers; free text (including JSON-looking text) is not proof."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict):
        return {key: _numeric_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numeric_data(item) for item in value]
    return None


def validate_report_narratives(artifacts: list[Artifact], args: JsonObject) -> JsonObject:
    evidence: list[EvidenceRecord] = []
    sections = [("title", args["title"], True), ("insights", args.get("insights", ""), False)]
    source_hashes: dict[str, str] = {}
    for artifact in artifacts:
        payload = artifact.payload or {}
        if artifact.type == "profile":
            data = payload.get("profile", payload)
        elif artifact.type == "stats":
            data = payload.get("result", payload)
        elif artifact.type == "chart":
            # ECharts style/layout numbers and model-authored labels are not data.
            option = payload.get("option") or {}
            data = {
                "series": [
                    series.get("data", [])
                    for series in option.get("series", [])
                    if isinstance(series, dict)
                ]
            }
            titles = option.get("title") or []
            if isinstance(titles, dict):
                titles = [titles]
            for index, title in enumerate(titles):
                if isinstance(title, dict):
                    for key in ("text", "subtext"):
                        if title.get(key):
                            sections.append(
                                (f"{artifact.id}.title[{index}].{key}", str(title[key]), True)
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
        result_hash = hashlib.sha256(
            json.dumps(numeric_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        source_hashes[artifact.id] = result_hash
        evidence.append(
            EvidenceRecord(
                evidence_id=artifact.id,
                run_id="report-validation",
                invocation_id="",
                artifact_id=artifact.id,
                kind=artifact.type,
                source={"artifact_id": artifact.id},
                result_hash=result_hash,
                created_at=artifact.created_at,
                summary=build_evidence_summary(
                    summary="", result=numeric_data, artifact_id=artifact.id
                ),
            )
        )

    verified: list[JsonObject] = []
    for name, text, heading in sections:
        if not text:
            continue
        for claim in extract_numeric_claims(
            final_text=_visible_text(text, heading=heading), goal="", evidence=evidence
        ):
            unsupported = [ref["token"] for ref in claim.value_refs if not ref.get("supported")]
            if unsupported:
                raise ValueError(
                    f"报告正文数值缺少所选分析的证据（{name}: {', '.join(unsupported[:5])}）；"
                    "请使用工具计算并选择对应分析，或删除无法核实的数字后重试"
                )
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
    return {
        "status": "passed",
        "validator": "selected-artifact-numeric-v1",
        "source_hashes": source_hashes,
        "claims": verified,
    }
