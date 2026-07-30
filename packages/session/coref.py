"""受项目隔离约束的确定性 Artifact/Dataset 指代消解。

阶段 3C-1 不调用模型，也不从摘要猜实体。解析器只接受当前主体可见的项目、对话、
Artifact 和 Dataset，并把确定命中的 opaque reference 作为 Host 注解追加到查询。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from packages.governance.audit import AuditEvent, AuditOutcome
from packages.governance.audit import record as record_audit
from packages.governance.permissions import Principal
from packages.session.models import Artifact, Dataset
from packages.session.store import SessionStore

REFERENCE_POLICY_VERSION = "coref-policy-v1"
REFERENCE_ASSUMPTION_PREFIX = "HOST_COREF_V1:"
_HOST_ANNOTATION_PREFIX = "[Host 已验证引用 coref-v1]"
_MAX_QUERY_CHARS = 20_000
_MAX_REFERENCE_TARGETS = 5
_COMPACT_BINDING_VERSION = "1"
_ANALYSIS_ID_PATTERN = re.compile(
    r"(?:(?:analysis[_ ]?id|dataset[_ ]?ref)\s*[:=]\s*)?"
    r"([0-9a-f]{32}|[0-9a-f]{12})(?![0-9a-f])",
    re.I,
)
_MEMORY_ID_PATTERN = re.compile(
    r"memory[_ ]?id\s*[:=]\s*[0-9a-f]{32}(?![0-9a-f])",
    re.I,
)
_ORDINAL_PATTERN = re.compile(
    r"第(?P<number>[一二三四五六七八九十百两\d]+)"
    r"(?:张|个|份|条)?(?P<kind>图表?|数据集|数据|报告|表格|结果)"
)
_RECENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "chart",
        re.compile(r"(?:刚才|上次|最新|上一张)(?:生成的|的)?(?:那张|这个)?图(?:表)?"),
    ),
    (
        "report",
        re.compile(r"(?:刚才|上次|最新)(?:生成的|的)?(?:那份|这个)?报告"),
    ),
    (
        "table",
        re.compile(r"(?:刚才|上次|最新)(?:生成的|的)?(?:那张|这个)?表(?:格)?"),
    ),
    (
        "dataset",
        re.compile(r"(?:刚才|上次|当前|最新)(?:使用的|上传的|的)?(?:这个)?数据集"),
    ),
)
_DEICTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("chart", re.compile(r"(?:这个|那个|那张|上述)(?:图|图表)")),
    ("report", re.compile(r"(?:这个|那个|那份|上述)(?:报告)")),
    ("table", re.compile(r"(?:这个|那个|那张|上述)(?:表|表格)")),
    ("dataset", re.compile(r"(?:这个|那个|上述)(?:数据集)")),
    ("result", re.compile(r"(?:这个|那个|上述)(?:结果|分析)")),
)
_ARTIFACT_KIND_MAP = {
    "图": "chart",
    "图表": "chart",
    "报告": "report",
    "表": "table",
    "表格": "table",
    "结果": "result",
}
_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

ReferenceStatus = Literal["no_reference", "resolved", "ambiguous", "unresolved"]
ReferenceKind = Literal["artifact", "dataset"]


class ReferenceAccessDenied(PermissionError):
    """当前主体不能读取指定项目或对话的引用目录。"""


@dataclass(frozen=True, slots=True)
class ReferenceTarget:
    """一个由 Host 验证过的最小引用目标。"""

    kind: ReferenceKind
    reference_id: str
    mention: str
    artifact_type: str | None = None
    analysis_id: str | None = None
    dataset_ref: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "reference_id": self.reference_id,
            "mention": self.mention,
            "artifact_type": self.artifact_type,
            "analysis_id": self.analysis_id,
            "dataset_ref": self.dataset_ref,
        }

    def binding_dict(self) -> dict[str, str | None]:
        """返回可持久化的 opaque 绑定，不保存用户原始提及文本。"""
        return {
            "kind": self.kind,
            "reference_id": self.reference_id,
            "artifact_type": self.artifact_type,
            "analysis_id": self.analysis_id,
            "dataset_ref": self.dataset_ref,
        }


@dataclass(frozen=True, slots=True)
class ReferenceChoice:
    """歧义澄清时可公开的最小候选。"""

    kind: ReferenceKind
    reference_id: str
    label: str
    position: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "kind": self.kind,
            "reference_id": self.reference_id,
            "label": self.label,
            "position": self.position,
        }


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    """一次确定性解析结果；非 resolved 状态不生成 Host 注解。"""

    status: ReferenceStatus
    original_query: str
    rewritten_query: str
    targets: tuple[ReferenceTarget, ...]
    choices: tuple[ReferenceChoice, ...]
    reason_code: str | None
    resolution_hash: str

    @property
    def has_reference(self) -> bool:
        return self.status != "no_reference"

    def assumption(self) -> str | None:
        """生成可随 TaskPlan 持久化且不含业务正文的恢复绑定。"""
        if self.status != "resolved":
            return None
        payload = {
            "h": self.resolution_hash,
            "p": _COMPACT_BINDING_VERSION,
            "t": [
                ("a" if target.kind == "artifact" else "d") + target.reference_id
                for target in self.targets
            ],
        }
        return REFERENCE_ASSUMPTION_PREFIX + _stable_json(payload)

    def clarification(self) -> dict[str, object] | None:
        """把歧义转换为现有 Planner 可持久化的阻塞澄清契约。"""
        if self.status not in {"ambiguous", "unresolved"}:
            return None
        choices = [choice.to_dict() for choice in self.choices]
        if self.reason_code == "reference_limit_exceeded":
            question = (
                f"一次最多可绑定 {_MAX_REFERENCE_TARGETS} 个引用目标，" "请缩小范围或分批处理。"
            )
        elif choices:
            prefix = "该引用有多个可能目标，请明确选择："
            labels: list[str] = []
            for choice in self.choices[:8]:
                label = f"{choice.position}. {choice.label}" f"（{choice.reference_id}）"
                if len(prefix + "；".join([*labels, label])) > 500:
                    break
                labels.append(label)
            question = prefix + "；".join(labels)
        else:
            question = "没有找到该引用对应的当前对话对象，请提供明确的序号或引用 ID。"
        return {
            "question_id": "reference_target",
            "about": "reference_target",
            "question": question,
            "reason": "猜测引用目标可能使用错误数据或工件。",
            "blocking": True,
            "choices": choices,
            "reason_code": self.reason_code,
        }


class ReferenceResolver:
    """从 SQLite 真相源构建当前作用域引用目录并确定性解析。"""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        audit_recorder: Callable[[AuditEvent], None] = record_audit,
    ) -> None:
        self._store = session_store
        self._audit_recorder = audit_recorder

    def resolve(
        self,
        query: str,
        *,
        project_id: str,
        conversation_id: str,
        principal: Principal,
    ) -> ReferenceResolution:
        """解析查询中的 Artifact/Dataset 指代；歧义和越界均失败关闭。"""
        clean = _clean_query(query)
        try:
            artifacts, datasets = self._catalog(
                project_id=project_id,
                conversation_id=conversation_id,
                principal=principal,
            )
            resolution = _resolve(clean, artifacts=artifacts, datasets=datasets)
        except Exception as exc:
            self._audit(
                project_id=project_id,
                conversation_id=conversation_id,
                principal=principal,
                outcome=(
                    "denied" if isinstance(exc, ReferenceAccessDenied | ValueError) else "error"
                ),
                detail={"reason_code": type(exc).__name__},
            )
            raise
        self._audit(
            project_id=project_id,
            conversation_id=conversation_id,
            principal=principal,
            outcome="allowed",
            detail={
                "status": resolution.status,
                "reason_code": resolution.reason_code,
                "resolution_hash": resolution.resolution_hash,
                "target_ids": [target.reference_id for target in resolution.targets],
                "candidate_count": len(resolution.choices),
            },
        )
        return resolution

    def restore(
        self,
        assumption: str,
        *,
        query: str,
        project_id: str,
        conversation_id: str,
        principal: Principal,
    ) -> ReferenceResolution:
        """从持久化 Host assumption 恢复并重新验证目标归属与 hash。"""
        clean = _clean_query(query)
        if not assumption.startswith(REFERENCE_ASSUMPTION_PREFIX):
            raise ValueError("指代恢复契约前缀无效")
        try:
            payload = json.loads(assumption.removeprefix(REFERENCE_ASSUMPTION_PREFIX))
        except json.JSONDecodeError as exc:
            raise ValueError("指代恢复契约 JSON 无效") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("p") != _COMPACT_BINDING_VERSION
            or not isinstance(payload.get("t"), list)
        ):
            raise ValueError("指代恢复契约版本或目标无效")
        artifacts, datasets = self._catalog(
            project_id=project_id,
            conversation_id=conversation_id,
            principal=principal,
        )
        artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
        datasets_by_ref = {dataset.ref: dataset for dataset in datasets}
        restored: list[ReferenceTarget] = []
        raw_targets = payload["t"]
        if not 1 <= len(raw_targets) <= _MAX_REFERENCE_TARGETS:
            raise ValueError("指代恢复目标数量无效")
        for raw in raw_targets:
            if not isinstance(raw, str) or len(raw) != 33 or raw[0] not in {"a", "d"}:
                raise ValueError("指代恢复目标字段无效")
            reference_id = raw[1:]
            if raw[0] == "a":
                artifact = artifacts_by_id.get(reference_id)
                if artifact is None:
                    raise ReferenceAccessDenied("指代目标不存在")
                target = _artifact_target(artifact, "persisted_reference")
            else:
                dataset = datasets_by_ref.get(reference_id)
                if dataset is None:
                    raise ReferenceAccessDenied("指代目标不存在")
                target = _dataset_target(dataset, "persisted_reference")
            restored.append(target)
        resolution = _resolved(clean, restored)
        if payload.get("h") != resolution.resolution_hash:
            raise ReferenceAccessDenied("指代恢复 hash 不一致")
        return resolution

    def _catalog(
        self,
        *,
        project_id: str,
        conversation_id: str,
        principal: Principal,
    ) -> tuple[list[Artifact], list[Dataset]]:
        role = self._store.project_role(
            project_id,
            user_id=principal.user_id,
            tenant_id=principal.tenant_scope,
        )
        conversation = self._store.get_conversation(conversation_id)
        if role is None or conversation is None or conversation.project_id != project_id:
            raise ReferenceAccessDenied("指代目录不存在")
        return (
            self._store.list_artifacts(conversation_id),
            self._store.list_datasets(project_id),
        )

    def _audit(
        self,
        *,
        project_id: str,
        conversation_id: str,
        principal: Principal,
        outcome: AuditOutcome,
        detail: dict[str, object],
    ) -> None:
        self._audit_recorder(
            AuditEvent(
                actor=principal.user_id,
                tenant_id=principal.tenant_scope,
                action="reference.resolve",
                resource="conversation_context",
                outcome=outcome,
                project_id=project_id,
                detail={"conversation_id": conversation_id, **detail},
            )
        )


def find_reference_assumption(values: object) -> str | None:
    """从 TaskContract/TaskPlan assumptions 中提取唯一 Host 指代绑定。"""
    if not isinstance(values, list | tuple):
        return None
    matches = [
        value
        for value in values
        if isinstance(value, str) and value.startswith(REFERENCE_ASSUMPTION_PREFIX)
    ]
    if len(matches) > 1:
        raise ValueError("持久化计划包含多个指代绑定")
    return matches[0] if matches else None


def _resolve(
    query: str,
    *,
    artifacts: list[Artifact],
    datasets: list[Dataset],
) -> ReferenceResolution:
    targets: list[ReferenceTarget] = []
    issues: list[tuple[str, list[ReferenceChoice]]] = []
    consumed: list[tuple[int, int]] = []

    artifacts_by_analysis = {_analysis_id(artifact): artifact for artifact in artifacts}
    artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
    datasets_by_ref = {dataset.ref: dataset for dataset in datasets}
    memory_id_spans = [match.span() for match in _MEMORY_ID_PATTERN.finditer(query)]
    explicit_matches = [
        match
        for match in _ANALYSIS_ID_PATTERN.finditer(query)
        if not _overlaps(match.span(), memory_id_spans)
    ]
    for match in explicit_matches:
        token = match.group(1).lower()
        artifact = artifacts_by_analysis.get(token) or artifacts_by_id.get(token)
        dataset = datasets_by_ref.get(token)
        if artifact is not None:
            targets.append(_artifact_target(artifact, match.group(0)))
            consumed.append(match.span())
        elif dataset is not None:
            targets.append(_dataset_target(dataset, match.group(0)))
            consumed.append(match.span())

    for dataset in datasets:
        if dataset.filename and dataset.filename in query:
            same_name = [item for item in datasets if item.filename == dataset.filename]
            if len(same_name) == 1:
                targets.append(_dataset_target(dataset, dataset.filename))
            else:
                issues.append(
                    (
                        "duplicate_dataset_filename",
                        _dataset_choices(same_name),
                    )
                )

    for match in _ORDINAL_PATTERN.finditer(query):
        if _overlaps(match.span(), consumed):
            continue
        position = _ordinal_value(match.group("number"))
        kind_text = match.group("kind")
        mention = match.group(0)
        if kind_text in {"数据集", "数据"}:
            candidates: list[Artifact] | list[Dataset] = datasets
            choices = _dataset_choices(datasets)
        else:
            artifact_type = _ARTIFACT_KIND_MAP[kind_text]
            candidates = _artifact_candidates(artifacts, artifact_type)
            choices = _artifact_choices(candidates)
        if position is None or position < 1 or position > len(candidates):
            issues.append(("reference_ordinal_out_of_range", choices))
            continue
        selected = candidates[position - 1]
        targets.append(
            _artifact_target(selected, mention)
            if isinstance(selected, Artifact)
            else _dataset_target(selected, mention)
        )
        consumed.append(match.span())

    for selector, pattern in _RECENT_PATTERNS:
        for match in pattern.finditer(query):
            if selector == "dataset":
                if not datasets:
                    issues.append(("reference_not_found", []))
                    continue
                targets.append(_dataset_target(datasets[-1], match.group(0)))
                consumed.append(match.span())
                continue
            artifact_candidates = _artifact_candidates(artifacts, selector)
            if not artifact_candidates:
                issues.append(("reference_not_found", []))
                continue
            targets.append(_artifact_target(artifact_candidates[-1], match.group(0)))
            consumed.append(match.span())

    if "刚才" in query or "上次" in query:
        for token, selector in (("趋势", "trend"), ("图表", "chart")):
            if token not in query:
                continue
            candidates = _artifact_candidates(artifacts, selector)
            if candidates:
                targets.append(_artifact_target(candidates[-1], f"刚才的{token}"))
            else:
                issues.append(("reference_not_found", []))

    for selector, pattern in _DEICTIC_PATTERNS:
        for match in pattern.finditer(query):
            if _overlaps(match.span(), consumed):
                continue
            if any(_target_matches_selector(target, selector) for target in targets):
                consumed.append(match.span())
                continue
            if selector == "dataset":
                if len(datasets) == 1:
                    targets.append(_dataset_target(datasets[0], match.group(0)))
                elif datasets:
                    issues.append(("reference_ambiguous", _dataset_choices(datasets)))
                else:
                    issues.append(("reference_not_found", []))
                continue
            artifact_candidates = _artifact_candidates(artifacts, selector)
            if len(artifact_candidates) == 1:
                targets.append(_artifact_target(artifact_candidates[0], match.group(0)))
            elif artifact_candidates:
                issues.append(
                    (
                        "reference_ambiguous",
                        _artifact_choices(artifact_candidates),
                    )
                )
            else:
                issues.append(("reference_not_found", []))

    targets = _deduplicate_targets(targets)
    if issues:
        reason_code = issues[0][0]
        choices = _deduplicate_choices(
            [choice for _, issue_choices in issues for choice in issue_choices]
        )
        status: ReferenceStatus = (
            "ambiguous"
            if any(
                code in {"reference_ambiguous", "duplicate_dataset_filename"} for code, _ in issues
            )
            else "unresolved"
        )
        return _result(
            status=status,
            query=query,
            targets=(),
            choices=tuple(choices),
            reason_code=reason_code,
        )
    if len(targets) > _MAX_REFERENCE_TARGETS:
        return _result(
            status="unresolved",
            query=query,
            targets=(),
            choices=(),
            reason_code="reference_limit_exceeded",
        )
    if targets:
        return _resolved(query, targets)
    if explicit_matches or _contains_reference_signal(query):
        return _result(
            status="unresolved",
            query=query,
            targets=(),
            choices=(),
            reason_code="reference_not_found",
        )
    return _result(
        status="no_reference",
        query=query,
        targets=(),
        choices=(),
        reason_code=None,
    )


def _resolved(query: str, targets: list[ReferenceTarget]) -> ReferenceResolution:
    return _result(
        status="resolved",
        query=query,
        targets=tuple(_deduplicate_targets(targets)),
        choices=(),
        reason_code=None,
    )


def _result(
    *,
    status: ReferenceStatus,
    query: str,
    targets: tuple[ReferenceTarget, ...],
    choices: tuple[ReferenceChoice, ...],
    reason_code: str | None,
) -> ReferenceResolution:
    payload = {
        "policy_version": REFERENCE_POLICY_VERSION,
        "status": status,
        "targets": [target.binding_dict() for target in targets],
        "reason_code": reason_code,
    }
    resolution_hash = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    annotation = (
        "\n\n"
        + _HOST_ANNOTATION_PREFIX
        + " "
        + _stable_json(
            {
                "resolution_hash": resolution_hash,
                "targets": [target.binding_dict() for target in targets],
            }
        )
        if status == "resolved"
        else ""
    )
    return ReferenceResolution(
        status=status,
        original_query=query,
        rewritten_query=query + annotation,
        targets=targets,
        choices=choices,
        reason_code=reason_code,
        resolution_hash=resolution_hash,
    )


def _artifact_candidates(
    artifacts: list[Artifact],
    selector: str,
) -> list[Artifact]:
    if selector == "result":
        return [
            artifact
            for artifact in artifacts
            if artifact.type in {"stats", "chart", "table", "report"}
        ]
    if selector == "trend":
        return [
            artifact
            for artifact in artifacts
            if artifact.type == "stats" and artifact.source_tool == "trend_analysis"
        ]
    return [artifact for artifact in artifacts if artifact.type == selector]


def _artifact_target(artifact: Artifact, mention: str) -> ReferenceTarget:
    return ReferenceTarget(
        kind="artifact",
        reference_id=artifact.id,
        mention=mention,
        artifact_type=artifact.type,
        analysis_id=_analysis_id(artifact),
        dataset_ref=artifact.dataset_ref,
    )


def _target_matches_selector(target: ReferenceTarget, selector: str) -> bool:
    if selector == "dataset":
        return target.kind == "dataset"
    if target.kind != "artifact":
        return False
    if selector == "result":
        return target.artifact_type in {"stats", "chart", "table", "report"}
    if selector == "trend":
        return target.artifact_type == "stats"
    return target.artifact_type == selector


def _dataset_target(dataset: Dataset, mention: str) -> ReferenceTarget:
    return ReferenceTarget(
        kind="dataset",
        reference_id=dataset.ref,
        mention=mention,
        dataset_ref=dataset.ref,
    )


def _analysis_id(artifact: Artifact) -> str:
    params = artifact.params or {}
    value = params.get("analysis_id")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return artifact.id


def _artifact_choices(artifacts: list[Artifact]) -> list[ReferenceChoice]:
    return [
        ReferenceChoice(
            kind="artifact",
            reference_id=artifact.id,
            label=(
                f"{artifact.type} / analysis_id={_analysis_id(artifact)}"
                f" / dataset_ref={artifact.dataset_ref or '-'}"
            ),
            position=position,
        )
        for position, artifact in enumerate(artifacts, 1)
    ]


def _dataset_choices(datasets: list[Dataset]) -> list[ReferenceChoice]:
    return [
        ReferenceChoice(
            kind="dataset",
            reference_id=dataset.ref,
            label=f"{dataset.filename} / dataset_ref={dataset.ref}",
            position=position,
        )
        for position, dataset in enumerate(datasets, 1)
    ]


def _deduplicate_targets(targets: list[ReferenceTarget]) -> list[ReferenceTarget]:
    seen: set[tuple[str, str]] = set()
    result: list[ReferenceTarget] = []
    for target in targets:
        key = (target.kind, target.reference_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result


def _deduplicate_choices(choices: list[ReferenceChoice]) -> list[ReferenceChoice]:
    seen: set[tuple[str, str]] = set()
    result: list[ReferenceChoice] = []
    for choice in choices:
        key = (choice.kind, choice.reference_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(choice)
    return result


def _ordinal_value(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        unit = _CHINESE_DIGITS.get(value[1])
        return 10 + unit if unit is not None else None
    if value.endswith("十") and len(value) == 2:
        tens = _CHINESE_DIGITS.get(value[0])
        return tens * 10 if tens is not None else None
    if "十" in value and len(value) == 3:
        tens = _CHINESE_DIGITS.get(value[0])
        unit = _CHINESE_DIGITS.get(value[2])
        return tens * 10 + unit if tens is not None and unit is not None else None
    return _CHINESE_DIGITS.get(value)


def _contains_reference_signal(query: str) -> bool:
    tokens = (
        "这个图",
        "那个图",
        "那张图",
        "上述图",
        "这个数据集",
        "那个数据集",
        "这个结果",
        "那个结果",
        "刚才的",
        "上次的",
        "上一张",
    )
    return any(token in query for token in tokens)


def _overlaps(span: tuple[int, int], consumed: list[tuple[int, int]]) -> bool:
    return any(span[0] < other[1] and other[0] < span[1] for other in consumed)


def _clean_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    clean = query.strip()
    if not clean:
        raise ValueError("query 不能为空")
    if len(clean) > _MAX_QUERY_CHARS:
        raise ValueError(f"query 超过 {_MAX_QUERY_CHARS} 字符")
    return clean


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
