"""Deterministic, profile-only candidate hypotheses for open exploration.

The screening contract is deliberately narrower than execution planning: it
proposes bounded, untested questions from governed profile metadata and the
frozen capability catalog.  It never reads raw rows or presents a candidate as
an analytical conclusion.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from mcp_servers.common.contracts import stable_hash
from mcp_servers.excel_parser.advisor import infer_data_roles_from_mapping
from packages.session.models import Dataset, JsonObject

HypothesisStatus = Literal["eligible", "needs_confirmation", "rejected"]

HYPOTHESIS_SCREENING_SCHEMA = "chatbi-hypothesis-screening-v1"
_OPEN_EXPLORATION_PATTERN = re.compile(
    r"^(?:请)?(?:深入|全面|详细)?(?:分析|看看|看一下|研究)(?:一下)?(?:这份|这个)?数据[。！!？?]?$"
)
_CANDIDATE_LIMIT = 4

_SCREENING_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "triggered",
        "data_version_hash",
        "dataset_ref",
        "candidate_limit",
        "candidates",
        "eligible_candidate_ids",
        "requires_confirmation",
        "blocking_reason",
        "raw_rows_read",
    ],
    "properties": {
        "schema": {"const": HYPOTHESIS_SCREENING_SCHEMA},
        "schema_version": {"const": 1},
        "triggered": {"type": "boolean"},
        "data_version_hash": {"type": "string", "minLength": 1, "maxLength": 128},
        "dataset_ref": {"type": ["string", "null"], "maxLength": 256},
        "candidate_limit": {"type": "integer", "minimum": 1, "maximum": 4},
        "candidates": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "hypothesis_id",
                    "kind",
                    "statement",
                    "capability",
                    "required_roles",
                    "expected_evidence",
                    "status",
                    "reason_codes",
                    "priority",
                    "tested",
                ],
                "properties": {
                    "hypothesis_id": {
                        "type": "string",
                        "pattern": "^hyp_[0-9a-f]{16}$",
                    },
                    "kind": {
                        "enum": [
                            "trend",
                            "anomaly",
                            "segment_comparison",
                            "correlation",
                        ]
                    },
                    "statement": {"type": "string", "minLength": 1, "maxLength": 300},
                    "capability": {"type": "string", "minLength": 1, "maxLength": 100},
                    "required_roles": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["role", "columns"],
                            "properties": {
                                "role": {"enum": ["time", "metric", "dimension"]},
                                "columns": {
                                    "type": "array",
                                    "maxItems": 8,
                                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                                },
                            },
                        },
                    },
                    "expected_evidence": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                    },
                    "status": {"enum": ["eligible", "needs_confirmation", "rejected"]},
                    "reason_codes": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 100},
                    },
                    "priority": {"type": "integer", "minimum": 1, "maximum": 4},
                    "tested": {"const": False},
                },
            },
        },
        "eligible_candidate_ids": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "pattern": "^hyp_[0-9a-f]{16}$"},
        },
        "requires_confirmation": {"type": "boolean"},
        "blocking_reason": {
            "enum": [
                "not_open_exploration",
                "dataset_required",
                "dataset_selection_required",
                "profile_unavailable",
                "no_eligible_candidates",
                "user_selection_required",
            ]
        },
        "raw_rows_read": {"const": False},
    },
}
_SCREENING_VALIDATOR = Draft202012Validator(_SCREENING_SCHEMA)


def requests_open_exploration(user_text: str) -> bool:
    """Return whether the request intentionally leaves the analysis goal open."""
    return _OPEN_EXPLORATION_PATTERN.search(user_text.strip()) is not None


def screen_candidate_hypotheses(
    *,
    user_text: str,
    datasets: list[Dataset],
    capability_catalog: list[JsonObject],
    data_version_hash: str,
    verified_dataset_refs: frozenset[str] = frozenset(),
    candidate_limit: int = _CANDIDATE_LIMIT,
) -> JsonObject | None:
    """Generate and screen a bounded set of profile-backed candidate hypotheses.

    ``None`` means the request is not an open exploration request.  A triggered
    contract with no candidates explains why screening failed closed.
    """
    if not requests_open_exploration(user_text):
        return None
    limit = min(max(candidate_limit, 1), _CANDIDATE_LIMIT)
    dataset, blocking_reason = _select_dataset(datasets, verified_dataset_refs)
    if dataset is None:
        return _validated_screening(
            _screening_payload(
                data_version_hash=data_version_hash,
                dataset_ref=None,
                candidate_limit=limit,
                candidates=[],
                blocking_reason=blocking_reason,
            )
        )

    try:
        inferred = infer_data_roles_from_mapping(dataset.profile, dataset_ref=dataset.ref)
    except ValueError:
        return _validated_screening(
            _screening_payload(
                data_version_hash=data_version_hash,
                dataset_ref=dataset.ref,
                candidate_limit=limit,
                candidates=[],
                blocking_reason="profile_unavailable",
            )
        )

    roles = _role_columns(inferred)
    allowed_capabilities = {
        str(item.get("name"))
        for item in capability_catalog
        if isinstance(item.get("name"), str) and item.get("allowed") is not False
    }
    candidates = [
        _candidate(
            dataset_ref=dataset.ref,
            data_version_hash=data_version_hash,
            kind="trend",
            capability="stats.trend",
            priority=1,
            role_requirements=(("time", 1), ("metric", 1)),
            roles=roles,
            allowed_capabilities=allowed_capabilities,
        ),
        _candidate(
            dataset_ref=dataset.ref,
            data_version_hash=data_version_hash,
            kind="anomaly",
            capability="stats.anomaly",
            priority=2,
            role_requirements=(("metric", 1),),
            roles=roles,
            allowed_capabilities=allowed_capabilities,
        ),
        _candidate(
            dataset_ref=dataset.ref,
            data_version_hash=data_version_hash,
            kind="segment_comparison",
            capability="data.aggregate",
            priority=3,
            role_requirements=(("dimension", 1), ("metric", 1)),
            roles=roles,
            allowed_capabilities=allowed_capabilities,
        ),
        _candidate(
            dataset_ref=dataset.ref,
            data_version_hash=data_version_hash,
            kind="correlation",
            capability="stats.correlation",
            priority=4,
            role_requirements=(("metric", 2),),
            roles=roles,
            allowed_capabilities=allowed_capabilities,
        ),
    ][:limit]
    eligible = [
        str(item["hypothesis_id"])
        for item in candidates
        if item["status"] == "eligible"
    ]
    return _validated_screening(
        _screening_payload(
            data_version_hash=data_version_hash,
            dataset_ref=dataset.ref,
            candidate_limit=limit,
            candidates=candidates,
            blocking_reason=(
                "user_selection_required" if eligible else "no_eligible_candidates"
            ),
        )
    )


def _select_dataset(
    datasets: list[Dataset], verified_dataset_refs: frozenset[str]
) -> tuple[Dataset | None, str]:
    verified = [dataset for dataset in datasets if dataset.ref in verified_dataset_refs]
    if len(verified) == 1:
        return verified[0], "user_selection_required"
    if not datasets:
        return None, "dataset_required"
    if len(datasets) > 1:
        return None, "dataset_selection_required"
    return datasets[0], "user_selection_required"


def _role_columns(inferred: JsonObject) -> dict[str, tuple[list[str], list[str]]]:
    unambiguous: dict[str, list[str]] = {"time": [], "metric": [], "dimension": []}
    ambiguous: dict[str, list[str]] = {"time": [], "metric": [], "dimension": []}
    raw_items = inferred.get("columns")
    items = cast(list[JsonObject], raw_items) if isinstance(raw_items, list) else []
    for item in items:
        column = item.get("column")
        if not isinstance(column, str) or not column:
            continue
        primary_role = item.get("primary_role")
        if primary_role in unambiguous and not bool(item.get("ambiguous")):
            unambiguous[str(primary_role)].append(column)
        raw_candidates = item.get("candidates")
        candidate_roles = {
            str(candidate.get("role"))
            for candidate in raw_candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("role"), str)
        } if isinstance(raw_candidates, list) else set()
        if bool(item.get("ambiguous")):
            for role in candidate_roles & set(ambiguous):
                ambiguous[role].append(column)
    return {
        role: (_unique(unambiguous[role]), _unique(ambiguous[role]))
        for role in unambiguous
    }


def _candidate(
    *,
    dataset_ref: str,
    data_version_hash: str,
    kind: str,
    capability: str,
    priority: int,
    role_requirements: tuple[tuple[str, int], ...],
    roles: dict[str, tuple[list[str], list[str]]],
    allowed_capabilities: set[str],
) -> JsonObject:
    required_roles: list[JsonObject] = []
    reason_codes: list[str] = []
    bindings: dict[str, list[str]] = {}
    needs_confirmation = False
    for role, count in role_requirements:
        unambiguous, ambiguous = roles[role]
        chosen = unambiguous[:count]
        if len(chosen) < count:
            confirmation_candidates = _unique([*chosen, *ambiguous])[:8]
            required_roles.append({"role": role, "columns": confirmation_candidates})
            bindings[role] = confirmation_candidates[:count]
            if len(confirmation_candidates) >= count:
                needs_confirmation = True
                reason_codes.append(f"{role}_confirmation_required")
            else:
                reason_codes.append(f"{role}_role_unavailable")
            continue
        required_roles.append({"role": role, "columns": chosen})
        bindings[role] = chosen
    if capability not in allowed_capabilities:
        reason_codes.append("capability_unavailable")

    role_unavailable = any(code.endswith("_role_unavailable") for code in reason_codes)
    if capability not in allowed_capabilities or role_unavailable:
        status: HypothesisStatus = "rejected"
    elif needs_confirmation:
        status = "needs_confirmation"
    else:
        status = "eligible"
        reason_codes.append("deterministic_profile_match")

    identity = {
        "data_version_hash": data_version_hash,
        "dataset_ref": dataset_ref,
        "kind": kind,
        "capability": capability,
        "bindings": bindings,
    }
    return {
        "hypothesis_id": f"hyp_{stable_hash(identity)[:16]}",
        "kind": kind,
        "statement": _statement(kind, bindings),
        "capability": capability,
        "required_roles": required_roles,
        "expected_evidence": _expected_evidence(kind),
        "status": status,
        "reason_codes": _unique(reason_codes),
        "priority": priority,
        "tested": False,
    }


def _statement(kind: str, bindings: dict[str, list[str]]) -> str:
    metric = _first(bindings.get("metric"), "指标")
    if kind == "trend":
        time = _first(bindings.get("time"), "时间字段")
        return f"{metric} 可能随 {time} 呈现时间变化，需要趋势 Evidence 验证。"
    if kind == "anomaly":
        return f"{metric} 可能存在异常观测，需要异常检测 Evidence 验证。"
    if kind == "segment_comparison":
        dimension = _first(bindings.get("dimension"), "分组维度")
        return f"不同 {dimension} 的 {metric} 可能存在差异，需要聚合 Evidence 验证。"
    metrics = bindings.get("metric") or []
    left = metrics[0] if metrics else "指标一"
    right = metrics[1] if len(metrics) > 1 else "指标二"
    return f"{left} 与 {right} 可能存在相关关系，需要相关性 Evidence 验证。"


def _expected_evidence(kind: str) -> str:
    return {
        "trend": "趋势统计量、时间范围与粒度",
        "anomaly": "异常方法、阈值与命中记录",
        "segment_comparison": "分组口径、聚合统计量与排序",
        "correlation": "相关系数、显著性与样本范围",
    }[kind]


def _screening_payload(
    *,
    data_version_hash: str,
    dataset_ref: str | None,
    candidate_limit: int,
    candidates: list[JsonObject],
    blocking_reason: str,
) -> JsonObject:
    eligible = [
        str(item["hypothesis_id"])
        for item in candidates
        if item.get("status") == "eligible"
    ]
    return {
        "schema": HYPOTHESIS_SCREENING_SCHEMA,
        "schema_version": 1,
        "triggered": True,
        "data_version_hash": data_version_hash,
        "dataset_ref": dataset_ref,
        "candidate_limit": candidate_limit,
        "candidates": candidates,
        "eligible_candidate_ids": eligible,
        "requires_confirmation": bool(eligible),
        "blocking_reason": blocking_reason,
        "raw_rows_read": False,
    }


def _validated_screening(payload: JsonObject) -> JsonObject:
    errors = sorted(_SCREENING_VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        error = best_match(errors)
        path = ".".join(str(part) for part in error.path) or "<root>"
        raise ValueError(f"候选假设契约校验失败 @ {path}: {error.message}")
    return payload


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _first(values: list[str] | None, fallback: str) -> str:
    return values[0] if values else fallback
