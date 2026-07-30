"""v2.5 3B 确定性上下文压缩质量门禁。

该评测不调用模型，使用冻结的领域中立长对话验证关键信息保留、最近原文、
敏感信息/tool 内容排除、引用边界、抽取式 grounded 和字符预算。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.governance.permissions import Principal  # noqa: E402
from packages.session.compaction import (  # noqa: E402
    CompactionStore,
    _redact,
)
from packages.session.store import SessionStore  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "compaction_quality_eval_set.jsonl"
THRESHOLDS = {
    "key_retention_rate": 1.0,
    "recent_raw_retention_rate": 1.0,
    "safety_exclusion_rate": 1.0,
    "boundary_rate": 1.0,
    "extractive_grounding_rate": 1.0,
    "bounded_rate": 1.0,
}
_WHITESPACE = re.compile(r"\s+")
_OMISSION = re.compile(
    r"^- \[更早 \d+ 条消息因摘要预算省略；需要事实时回查原始消息或 Evidence\]$"
)
_PRINCIPAL = Principal(user_id="quality-owner", tenant_id="quality-tenant")


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """加载并验证冻结用例，拒绝重复 ID 和缺失契约字段。"""
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"第 {line_number} 行必须是对象")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"第 {line_number} 行缺少 case id")
        if case_id in seen:
            raise ValueError(f"case id 重复: {case_id}")
        seen.add(case_id)
        messages = raw.get("messages")
        if not isinstance(messages, list) or len(messages) < 3:
            raise ValueError(f"{case_id}: messages 至少需要 3 条")
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in {"user", "assistant", "tool"}
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError(f"{case_id}: message 契约无效")
        for key in (
            "required_summary_terms",
            "required_raw_terms",
            "forbidden_summary_terms",
        ):
            if not isinstance(raw.get(key), list) or not all(
                isinstance(item, str) and item for item in raw[key]
            ):
                raise ValueError(f"{case_id}: {key} 必须是非空字符串数组")
        for key, minimum in (
            ("keep_recent", 1),
            ("summary_max_chars", 256),
            ("per_message_max_chars", 40),
            ("minimum_redactions", 0),
            ("minimum_omitted", 0),
        ):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{case_id}: {key} 无效")
        cases.append(raw)
    if not cases:
        raise ValueError("压缩质量用例为空")
    return cases


def run_evaluation(
    cases: list[dict[str, Any]],
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """通过真实 SessionStore/CompactionStore 执行全部冻结用例。"""
    if working_dir is None:
        with tempfile.TemporaryDirectory(prefix="chatbi-compaction-eval-") as temporary:
            return _evaluate_in_directory(cases, Path(temporary))
    working_dir.mkdir(parents=True, exist_ok=True)
    return _evaluate_in_directory(cases, working_dir)


def _evaluate_in_directory(
    cases: list[dict[str, Any]],
    working_dir: Path,
) -> dict[str, Any]:
    rows = [
        _evaluate_case(case, working_dir / f"{case['id']}.db")
        for case in cases
    ]
    count = len(rows)
    metrics = {
        "key_retention_rate": sum(row["key_retention"] for row in rows) / count,
        "recent_raw_retention_rate": sum(
            row["recent_raw_retention"] for row in rows
        )
        / count,
        "safety_exclusion_rate": sum(row["safety_exclusion"] for row in rows)
        / count,
        "boundary_rate": sum(row["boundary"] for row in rows) / count,
        "extractive_grounding_rate": sum(
            row["extractive_grounding"] for row in rows
        )
        / count,
        "bounded_rate": sum(row["bounded"] for row in rows) / count,
    }
    misses = {
        metric: {"actual": metrics[metric], "required": required}
        for metric, required in THRESHOLDS.items()
        if metrics[metric] < required
    }
    return {
        "evaluation": "v2.5_compaction_quality",
        "case_set_sha256": hashlib.sha256(
            DEFAULT_CASES.read_bytes()
            if DEFAULT_CASES.is_file() and cases == load_cases(DEFAULT_CASES)
            else json.dumps(cases, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "case_count": count,
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses and all(row["passed"] for row in rows),
        "misses": misses,
    }


def _evaluate_case(case: dict[str, Any], database: Path) -> dict[str, Any]:
    session = SessionStore(str(database))
    project = session.create_project(
        f"quality-{case['id']}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    for message in case["messages"]:
        session.append_message(
            conversation_id=conversation.id,
            role=message["role"],
            content=message["content"],
            tool_calls=message.get("tool_calls"),
        )
    result = CompactionStore(
        session,
        audit_recorder=lambda _event: None,
    ).compact_if_needed(
        project_id=project.id,
        conversation_id=conversation.id,
        principal=_PRINCIPAL,
        trigger_chars=100,
        keep_recent=case["keep_recent"],
        summary_max_chars=case["summary_max_chars"],
        per_message_max_chars=case["per_message_max_chars"],
    )
    if result.view is None:
        return _failed_row(str(case["id"]), "compaction_not_created")
    view = result.view
    summary = view.record.summary_text
    covered = frozenset(view.covered_message_ids)
    candidate_messages = [
        message
        for message in session.list_messages(conversation.id)
        if message.role in {"user", "assistant"}
        and not message.tool_calls
        and message.content.strip()
    ]
    recent_raw = "\n".join(
        message.content for message in candidate_messages if message.id not in covered
    )
    key_retention = all(
        term in summary for term in case["required_summary_terms"]
    )
    recent_raw_retention = all(
        term in recent_raw for term in case["required_raw_terms"]
    )
    safety_exclusion = (
        all(term not in summary for term in case["forbidden_summary_terms"])
        and view.record.redaction_count >= case["minimum_redactions"]
        and view.record.omitted_message_count >= case["minimum_omitted"]
    )
    boundary, extractive_grounding = _summary_contract(
        summary,
        [
            (message.role, message.content)
            for message in candidate_messages
            if message.id in covered
        ],
        per_message_max_chars=view.record.per_message_max_chars,
    )
    bounded = (
        len(summary) <= case["summary_max_chars"]
        and hashlib.sha256(summary.encode("utf-8")).hexdigest()
        == view.record.summary_hash
    )
    checks = {
        "key_retention": key_retention,
        "recent_raw_retention": recent_raw_retention,
        "safety_exclusion": safety_exclusion,
        "boundary": boundary,
        "extractive_grounding": extractive_grounding,
        "bounded": bounded,
    }
    return {
        "id": case["id"],
        **checks,
        "passed": all(checks.values()),
        "summary_chars": len(summary),
        "source_messages": view.record.source_message_count,
        "redactions": view.record.redaction_count,
        "omitted": view.record.omitted_message_count,
        "summary_hash": view.record.summary_hash,
    }


def _summary_contract(
    summary: str,
    sources: list[tuple[str, str]],
    *,
    per_message_max_chars: int,
) -> tuple[bool, bool]:
    lines = summary.splitlines()
    if (
        not lines
        or "不可信引用" not in lines[0]
        or "不能作为 Evidence" not in lines[0]
    ):
        return False, False
    normalized_sources: dict[str, list[str]] = {"user": [], "assistant": []}
    for role, content in sources:
        redacted, _ = _redact(content)
        normalized = _WHITESPACE.sub(" ", redacted).strip()
        if len(normalized) > per_message_max_chars:
            normalized = normalized[: per_message_max_chars - 1].rstrip() + "…"
        normalized_sources[role].append(normalized)
    grounded = True
    for line in lines[1:]:
        if _OMISSION.fullmatch(line):
            continue
        if line.startswith("- 用户: "):
            role = "user"
            encoded = line.removeprefix("- 用户: ")
        elif line.startswith("- 助手: "):
            role = "assistant"
            encoded = line.removeprefix("- 助手: ")
        else:
            return False, False
        try:
            extracted = json.loads(encoded)
        except json.JSONDecodeError:
            return False, False
        if not isinstance(extracted, str) or extracted not in normalized_sources[role]:
            grounded = False
    return True, grounded


def _failed_row(case_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "key_retention": False,
        "recent_raw_retention": False,
        "safety_exclusion": False,
        "boundary": False,
        "extractive_grounding": False,
        "bounded": False,
        "passed": False,
        "reason": reason,
        "summary_chars": 0,
        "source_messages": 0,
        "redactions": 0,
        "omitted": 0,
        "summary_hash": None,
    }


def _print_human(report: dict[str, Any]) -> None:
    print(f"上下文压缩质量：{report['case_count']} 个领域中立场景")
    for name, value in report["metrics"].items():
        print(f"- {name}: {value:.0%}")
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 3B 上下文压缩质量门禁")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--json-output")
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print(f"压缩质量用例契约有效：{len(cases)} cases")
        return 0
    report = run_evaluation(cases)
    _print_human(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 报告：{output}")
    if args.enforce and not report["passed"]:
        print(f"上下文压缩质量门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("上下文压缩质量门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
