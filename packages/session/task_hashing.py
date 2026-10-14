"""Stable hashes shared by TaskStore and orchestration callers."""

from __future__ import annotations

import hashlib
import json

from packages.session.models import JsonObject


def canonical_json(value: object) -> str:
    """Serialize a value using TaskStore's persisted canonical JSON format."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def invocation_arguments_hash(arguments: JsonObject) -> str:
    """Return the canonical hash persisted for tool invocation arguments."""
    return hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


def invocation_idempotency_key(
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: JsonObject,
) -> str:
    material = ":".join((run_id, tool_call_id, tool_name, canonical_json(arguments)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def business_invocation_key(run_id: str, tool_name: str, arguments: JsonObject) -> str:
    """同一 Run 的业务调用身份：不可变 dataset_ref/规范参数，不含模型调用或步骤 ID。

    Run 固定项目、主体和执行作用域。新数据版本使用新 dataset_ref；用户新回合
    重做使用新 run_id。未知结果不能靠生成新 call ID 获得新的执行许可。
    """
    return hashlib.sha256(
        canonical_json(
            {
                "schema": "business-invocation-v1",
                "run_id": run_id,
                "tool": tool_name,
                "arguments": arguments,
            }
        ).encode("utf-8")
    ).hexdigest()
