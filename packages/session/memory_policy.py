"""长期记忆的确定性内容、生命周期和快照策略。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime

from packages.session.memory_models import MemoryDraft

MEMORY_POLICY_VERSION = "memory-policy-v1"
MIN_MEMORY_SELECTION_CONFIDENCE = 0.70
MAX_MEMORY_SUMMARY_CHARS = 4_000
MAX_SEMANTIC_KEY_CHARS = 200
MAX_SOURCE_REF_CHARS = 200

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_PATH_RE = re.compile(
    r"(^|\s)(/home/|/root/|/etc/|[A-Za-z]:\\Users\\)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|authorization\s*:\s*bearer\s+\S+"
    r"|(?:api[_-]?key|password|secret)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


class MemoryPolicyViolation(ValueError):
    """候选记忆违反内容、来源或生命周期边界。"""


class MemoryPolicy:
    """规范化候选记忆，并拒绝凭据、宿主路径和不稳定时间。"""

    version = MEMORY_POLICY_VERSION
    minimum_selection_confidence = MIN_MEMORY_SELECTION_CONFIDENCE

    def normalize_draft(self, draft: MemoryDraft, *, now: str) -> MemoryDraft:
        """返回可持久化的规范候选；不执行项目授权或来源归属查询。"""
        semantic_key = _bounded_text(
            draft.semantic_key,
            "semantic_key",
            MAX_SEMANTIC_KEY_CHARS,
        )
        content_summary = _bounded_text(
            draft.content_summary,
            "content_summary",
            MAX_MEMORY_SUMMARY_CHARS,
        )
        source_ref = _bounded_text(
            draft.source_ref,
            "source_ref",
            MAX_SOURCE_REF_CHARS,
        )
        if _HOST_PATH_RE.search(content_summary):
            raise MemoryPolicyViolation("记忆摘要不得包含宿主机绝对路径")
        if _SECRET_RE.search(content_summary):
            raise MemoryPolicyViolation("记忆摘要疑似包含凭据或密钥")
        if not _SHA256_RE.fullmatch(draft.source_hash):
            raise MemoryPolicyViolation("source_hash 必须是 64 位小写 SHA-256")
        if (
            isinstance(draft.confidence, bool)
            or not isinstance(draft.confidence, int | float)
            or not 0.0 <= float(draft.confidence) <= 1.0
        ):
            raise MemoryPolicyViolation("confidence 必须在 0 到 1 之间")
        if draft.scope == "conversation" and draft.conversation_id is None:
            raise MemoryPolicyViolation("conversation scope 必须提供 conversation_id")
        if draft.scope != "conversation" and draft.conversation_id is not None:
            raise MemoryPolicyViolation("只有 conversation scope 可以绑定 conversation_id")

        valid_from = _normalize_timestamp(draft.valid_from or now, "valid_from")
        expires_at = (
            _normalize_timestamp(draft.expires_at, "expires_at")
            if draft.expires_at is not None
            else None
        )
        if expires_at is not None and expires_at <= valid_from:
            raise MemoryPolicyViolation("expires_at 必须晚于 valid_from")
        return replace(
            draft,
            semantic_key=semantic_key,
            content_summary=content_summary,
            source_ref=source_ref,
            source_hash=draft.source_hash.lower(),
            confidence=float(draft.confidence),
            valid_from=valid_from,
            expires_at=expires_at,
        )

    def selection_hash(
        self,
        *,
        tenant_id: str,
        project_id: str,
        subject_user_id: str,
        conversation_id: str | None,
        run_id: str | None,
        as_of: str,
    ) -> str:
        """计算选择条件 hash，避免把完整记忆正文写入审计元数据。"""
        payload = {
            "policy_version": self.version,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "subject_user_id": subject_user_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "as_of": as_of,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def normalize_as_of(value: str | None, *, now: str) -> str:
    """规范快照选择时间。"""
    return _normalize_timestamp(value or now, "as_of")


def _bounded_text(value: str, label: str, maximum: int) -> str:
    clean = value.strip()
    if not clean:
        raise MemoryPolicyViolation(f"{label} 不能为空")
    if len(clean) > maximum:
        raise MemoryPolicyViolation(f"{label} 超过 {maximum} 字符")
    if any(ord(char) < 32 and char not in {"\t", "\n", "\r"} for char in clean):
        raise MemoryPolicyViolation(f"{label} 包含控制字符")
    return clean


def _normalize_timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryPolicyViolation(f"{label} 不是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise MemoryPolicyViolation(f"{label} 必须包含时区")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
