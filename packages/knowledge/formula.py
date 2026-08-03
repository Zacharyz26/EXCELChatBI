"""Validate and compile the stage-5A metric formula DSL.

The DSL cannot contain SQL, code, paths, or arbitrary tool names. A valid
formula compiles deterministically into the existing ``aggregate_preview``
governed Tool contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from packages.knowledge.domain_models import CompiledInvocation
from packages.session.models import JsonObject

_SEMANTIC_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")

FORMULA_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "tool": {"const": "aggregate_preview"},
        "arguments": {
            "type": "object",
            "properties": {
                "group_concept": {"type": "string", "pattern": _SEMANTIC_KEY.pattern},
                "value_concept": {"type": "string", "pattern": _SEMANTIC_KEY.pattern},
                "agg": {"enum": ["sum", "mean", "count"]},
                "sort": {"enum": ["value_desc", "value_asc", "group"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["group_concept", "agg"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}

_VALIDATOR = Draft202012Validator(FORMULA_SCHEMA)


class FormulaMappingMissing(ValueError):
    """A valid formula cannot compile because required concept mappings are absent."""


def normalize_semantic_key(value: str, *, label: str = "语义键") -> str:
    """Normalize and validate a stable, domain-neutral semantic key."""
    normalized = value.strip().lower()
    if _SEMANTIC_KEY.fullmatch(normalized) is None:
        raise ValueError(f"{label}必须匹配 {_SEMANTIC_KEY.pattern}")
    return normalized


def normalize_formula(value: JsonObject) -> JsonObject:
    """Validate a formula and return a canonical JSON-compatible copy."""
    error = best_match(_VALIDATOR.iter_errors(value))
    if error is not None:
        path = ".".join(str(item) for item in error.absolute_path) or "$"
        raise ValueError(f"公式不符合受控契约（{path}）: {error.message}")
    arguments = cast(dict[str, Any], value["arguments"])
    agg = str(arguments["agg"])
    if agg != "count" and "value_concept" not in arguments:
        raise ValueError(f"公式 agg={agg} 必须声明 value_concept")
    normalized_arguments: JsonObject = {
        "group_concept": normalize_semantic_key(str(arguments["group_concept"])),
        "agg": agg,
    }
    if "value_concept" in arguments:
        normalized_arguments["value_concept"] = normalize_semantic_key(
            str(arguments["value_concept"])
        )
    if "sort" in arguments:
        normalized_arguments["sort"] = str(arguments["sort"])
    if "limit" in arguments:
        normalized_arguments["limit"] = int(arguments["limit"])
    return {"tool": "aggregate_preview", "arguments": normalized_arguments}


def formula_hash(formula: JsonObject) -> str:
    """Hash the normalized formula without depending on object key order."""
    payload = json.dumps(
        normalize_formula(formula),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compile_formula(
    *,
    definition_id: str,
    definition_version: int,
    formula: JsonObject,
    expected_formula_hash: str,
    dataset_ref: str,
    concept_fields: Mapping[str, str],
) -> CompiledInvocation:
    """Compile a trusted definition into an allowlisted Tool invocation."""
    normalized = normalize_formula(formula)
    actual_hash = formula_hash(normalized)
    if actual_hash != expected_formula_hash:
        raise ValueError("领域定义公式 hash 不匹配")
    raw_arguments = cast(dict[str, Any], normalized["arguments"])
    concepts = [str(raw_arguments["group_concept"])]
    if "value_concept" in raw_arguments:
        concepts.append(str(raw_arguments["value_concept"]))
    missing = [concept for concept in concepts if concept not in concept_fields]
    if missing:
        raise FormulaMappingMissing(f"数据集缺少领域字段映射: {', '.join(missing)}")

    arguments: JsonObject = {
        "dataset_ref": dataset_ref,
        "group_col": concept_fields[concepts[0]],
        "agg": str(raw_arguments["agg"]),
    }
    if "value_concept" in raw_arguments:
        arguments["value_col"] = concept_fields[str(raw_arguments["value_concept"])]
    if "sort" in raw_arguments:
        arguments["sort"] = str(raw_arguments["sort"])
    if "limit" in raw_arguments:
        arguments["limit"] = int(raw_arguments["limit"])
    return CompiledInvocation(
        definition_id=definition_id,
        definition_version=definition_version,
        formula_hash=actual_hash,
        tool_name="aggregate_preview",
        arguments=arguments,
    )
