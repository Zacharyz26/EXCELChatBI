"""Run the v2.4 constrained semantic Verifier evaluation suite.

Examples:

    .venv/bin/python scripts/agent_verifier_eval.py --validate-only
    .venv/bin/python scripts/agent_verifier_eval.py --repetitions 3 --enforce-hard
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.control.semantic_verifier import (  # noqa: E402
    PROMPT_VERSION,
    SemanticClaim,
    SemanticCriterion,
    SemanticEvaluation,
    SemanticEvidence,
    SemanticVerificationRequest,
    SemanticVerifier,
    SemanticVerifierProtocolError,
    validate_semantic_request,
)
from apps.orchestrator.control.verifier import VerificationResult  # noqa: E402
from packages.models.gateway import ModelGateway  # noqa: E402
from packages.models.registry import ModelRegistry  # noqa: E402
from packages.models.types import Scenario  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "semantic_verifier_eval_set.jsonl"
_EXPECTED_VERDICTS = {"PASS", "NEEDS_ACTION", "WAITING_USER"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"评测集第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"评测集第 {line_number} 行顶层必须是对象")
        case_id = _required_text(raw.get("id"), f"第 {line_number} 行 id")
        if case_id in seen:
            raise ValueError(f"评测 case id 重复: {case_id}")
        seen.add(case_id)
        split = _required_text(raw.get("split"), f"{case_id}.split")
        if split not in {"public", "heldout"}:
            raise ValueError(f"{case_id}.split 必须是 public 或 heldout")
        expected = _required_text(raw.get("expected_verdict"), f"{case_id}.expected")
        if expected not in _EXPECTED_VERDICTS:
            raise ValueError(f"{case_id}.expected_verdict 非法: {expected}")
        request = _request_from_mapping(raw.get("input"), case_id)
        validate_semantic_request(request)
        cases.append(
            {
                "id": case_id,
                "split": split,
                "category": _required_text(raw.get("category"), f"{case_id}.category"),
                "expected_verdict": expected,
                "request": request,
            }
        )
    if not cases:
        raise ValueError("语义 Verifier 评测集不能为空")
    return cases


async def run_evaluation(
    *,
    cases: list[dict[str, Any]],
    registry: ModelRegistry,
    model_names: list[str],
    repetitions: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for model_name in model_names:
        isolated = registry.isolated_route(
            Scenario.COMPLEX_REASONING,
            model_name,
            temperature=0.0,
        )
        verifier = SemanticVerifier(ModelGateway(isolated))
        for repetition in range(1, repetitions + 1):
            for case in cases:
                request = cast(SemanticVerificationRequest, case["request"])
                base: dict[str, Any] = {
                    "case_id": case["id"],
                    "split": case["split"],
                    "category": case["category"],
                    "repetition": repetition,
                    "configured_model": model_name,
                    "expected_verdict": case["expected_verdict"],
                    "request_hash": request.content_hash,
                }
                try:
                    evaluation = await verifier.evaluate(
                        request,
                        hard_result=VerificationResult(verdict="PASS"),
                    )
                except SemanticVerifierProtocolError as exc:
                    rows.append(
                        {
                            **base,
                            "predicted_verdict": "PROTOCOL_ERROR",
                            "matched": False,
                            "actual_model": exc.model,
                            "response_hash": exc.response_hash,
                            "prompt_tokens": exc.prompt_tokens,
                            "completion_tokens": exc.completion_tokens,
                            "latency_ms": round(exc.latency_ms, 3),
                            "cost": exc.cost,
                            "next_action": None,
                            "issue_codes": [],
                            "criterion_statuses": {},
                            "error_type": "protocol_error",
                            "error": str(exc),
                        }
                    )
                    continue
                except (RuntimeError, ValueError) as exc:
                    rows.append(
                        {
                            **base,
                            "predicted_verdict": "MODEL_ERROR",
                            "matched": False,
                            "actual_model": None,
                            "response_hash": None,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "latency_ms": 0.0,
                            "cost": None,
                            "next_action": None,
                            "issue_codes": [],
                            "criterion_statuses": {},
                            "error_type": "model_error",
                            "error": str(exc),
                        }
                    )
                    continue
                rows.append(_evaluation_row(base, evaluation))

    by_model = {
        model_name: _score_rows(
            [row for row in rows if row["configured_model"] == model_name]
        )
        for model_name in model_names
    }
    hard_no_go = any(
        score["false_passes"] > 0
        or score["protocol_errors"] > 0
        or score["model_errors"] > 0
        for score in by_model.values()
    )
    return {
        "schema_version": 1,
        "evaluation": "semantic_verifier",
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "repetitions": repetitions,
        "models": model_names,
        "case_count": len(cases),
        "metrics": by_model,
        "decision": "NO_GO" if hard_no_go else "REVIEW_REQUIRED",
        "decision_note": (
            "出现 false PASS、协议错误或模型错误，禁止接入生产语义门禁。"
            if hard_no_go
            else "硬性 no-go 条件未触发；仍需按评测设计完成人工盲评并冻结数值门槛。"
        ),
        "rows": rows,
    }


def _request_from_mapping(raw: object, case_id: str) -> SemanticVerificationRequest:
    if not isinstance(raw, dict):
        raise ValueError(f"{case_id}.input 必须是对象")
    criteria = tuple(
        SemanticCriterion(
            criterion_id=_required_text(item.get("criterion_id"), "criterion_id"),
            description=_required_text(item.get("description"), "criterion description"),
        )
        for item in _object_list(raw.get("criteria"), f"{case_id}.criteria")
    )
    claims = tuple(
        SemanticClaim(
            claim_id=_required_text(item.get("claim_id"), "claim_id"),
            text=_required_text(item.get("text"), "claim text"),
            evidence_ids=tuple(
                _string_list(item.get("evidence_ids"), "claim evidence_ids")
            ),
            limitations=tuple(
                _string_list(item.get("limitations", []), "claim limitations")
            ),
        )
        for item in _object_list(raw.get("claims"), f"{case_id}.claims")
    )
    evidence = tuple(
        SemanticEvidence(
            evidence_id=_required_text(item.get("evidence_id"), "evidence_id"),
            summary=_object(item.get("summary"), "evidence summary"),
        )
        for item in _object_list(raw.get("evidence"), f"{case_id}.evidence")
    )
    return SemanticVerificationRequest(
        goal=_required_text(raw.get("goal"), f"{case_id}.goal"),
        criteria=criteria,
        claims=claims,
        evidence=evidence,
        assumptions=tuple(_string_list(raw.get("assumptions", []), "assumptions")),
    )


def _evaluation_row(base: dict[str, Any], evaluation: SemanticEvaluation) -> dict[str, Any]:
    predicted = evaluation.verification.verdict
    return {
        **base,
        "predicted_verdict": predicted,
        "matched": predicted == base["expected_verdict"],
        "actual_model": evaluation.model,
        "response_hash": evaluation.response_hash,
        "prompt_tokens": evaluation.prompt_tokens,
        "completion_tokens": evaluation.completion_tokens,
        "latency_ms": round(evaluation.latency_ms, 3),
        "cost": evaluation.cost,
        "next_action": evaluation.next_action,
        "issue_codes": [
            issue.code for issue in evaluation.verification.issues
        ],
        "criterion_statuses": {
            item.criterion_id: item.status for item in evaluation.decisions
        },
        "error_type": None,
        "error": None,
    }


def _score_rows(rows: list[dict[str, Any]]) -> dict[str, int | float]:
    total = len(rows)
    matched = sum(bool(row["matched"]) for row in rows)
    return {
        "runs": total,
        "matched": matched,
        "exact_match_rate": matched / total if total else 0.0,
        "false_passes": sum(
            row["predicted_verdict"] == "PASS"
            and row["expected_verdict"] != "PASS"
            for row in rows
        ),
        "false_blocks": sum(
            row["predicted_verdict"] != "PASS"
            and row["expected_verdict"] == "PASS"
            for row in rows
        ),
        "protocol_errors": sum(row["error_type"] == "protocol_error" for row in rows),
        "model_errors": sum(row["error_type"] == "model_error" for row in rows),
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"Semantic Verifier prompt: {report['prompt_version']}")
    for model, metrics in cast(dict[str, dict[str, Any]], report["metrics"]).items():
        print(
            f"{model}: exact={metrics['exact_match_rate']:.1%} "
            f"false_pass={metrics['false_passes']} "
            f"false_block={metrics['false_blocks']} "
            f"protocol_error={metrics['protocol_errors']} "
            f"model_error={metrics['model_errors']}"
        )
    print(f"Decision: {report['decision']} — {report['decision_note']}")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return cast(dict[str, Any], value)


def _object_list(value: object, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} 必须是对象数组")
    return cast(list[dict[str, Any]], value)


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} 必须是字符串数组")
    return [str(item).strip() for item in value]


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.4 受约束语义 Verifier 评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--registry", default="config/models.yaml")
    parser.add_argument("--models", help="逗号分隔的 registry model name；默认评测整条 route")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--split", choices=("all", "public", "heldout"), default="all")
    parser.add_argument("--case-ids", help="逗号分隔的 case id；用于 smoke 或失败复跑")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce-hard", action="store_true")
    parser.add_argument("--json-output", help="报告路径；'-' 表示 stdout")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions 必须大于 0")

    cases = load_cases(args.cases)
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    if args.case_ids:
        requested_ids = {value.strip() for value in args.case_ids.split(",") if value.strip()}
        available_ids = {str(case["id"]) for case in cases}
        missing_ids = requested_ids - available_ids
        if missing_ids:
            parser.error(f"case id 不存在或不属于当前 split: {sorted(missing_ids)}")
        cases = [case for case in cases if case["id"] in requested_ids]
    if args.validate_only:
        print(f"Validated {len(cases)} semantic Verifier cases from {args.cases}")
        return 0

    registry = ModelRegistry(args.registry)
    registry.load()
    if args.models:
        model_names = [name.strip() for name in args.models.split(",") if name.strip()]
    else:
        model_names = list(registry.route_candidates(Scenario.COMPLEX_REASONING))
    if not model_names:
        parser.error("没有可评测模型")
    for model_name in model_names:
        registry.get_model(model_name)

    report = asyncio.run(
        run_evaluation(
            cases=cases,
            registry=registry,
            model_names=model_names,
            repetitions=args.repetitions,
        )
    )
    _print_report(report)
    if args.json_output == "-":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        output = (
            Path(args.json_output)
            if args.json_output
            else Path(".data/evaluations/v2.4")
            / f"semantic-{uuid.uuid4().hex[:12]}"
            / "report.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Report: {output}")
    return 1 if args.enforce_hard and report["decision"] == "NO_GO" else 0


if __name__ == "__main__":
    raise SystemExit(main())
