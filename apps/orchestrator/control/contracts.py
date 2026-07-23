"""TaskContract types and the conservative v2.4 stage-1 interpreter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Literal

from packages.session.models import JsonObject

CriterionKind = Literal["response", "artifact", "evidence", "semantic", "constraint"]


@dataclass(frozen=True, slots=True)
class SuccessCriterion:
    criterion_id: str
    kind: CriterionKind
    description: str
    required: bool = True
    artifact_type: str | None = None
    artifact_format: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "criterion_id": self.criterion_id,
            "kind": self.kind,
            "description": self.description,
            "required": self.required,
            "artifact_type": self.artifact_type,
            "artifact_format": self.artifact_format,
        }


@dataclass(frozen=True, slots=True)
class TaskContract:
    run_id: str
    goal: str
    success_criteria: tuple[SuccessCriterion, ...]
    constraints: tuple[str, ...]
    assumptions: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "success_criteria": [item.to_dict() for item in self.success_criteria],
            "constraints": list(self.constraints),
            "assumptions": list(self.assumptions),
        }

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def require_artifact(
        self, artifact_type: str, artifact_format: str | None = None
    ) -> TaskContract:
        criterion_id = f"artifact.{artifact_type}"
        if artifact_format:
            criterion_id += f".{artifact_format}"
        if any(item.criterion_id == criterion_id for item in self.success_criteria):
            return self
        criterion = SuccessCriterion(
            criterion_id=criterion_id,
            kind="artifact",
            description=f"生成真实的 {artifact_type} 工件",
            artifact_type=artifact_type,
            artifact_format=artifact_format,
        )
        return replace(self, success_criteria=(*self.success_criteria, criterion))


def build_minimal_contract(
    *,
    run_id: str,
    user_text: str,
    chart_required: bool,
    report_required: bool,
    pdf_required: bool,
) -> TaskContract:
    """Compile only high-confidence requirements; stage 2 adds the full interpreter."""
    contract = TaskContract(
        run_id=run_id,
        goal=user_text.strip(),
        success_criteria=(
            SuccessCriterion(
                criterion_id="response.non_empty",
                kind="response",
                description="交付非空的最终答复",
            ),
        ),
        constraints=(
            "任务只有在 Verifier 通过后才能完成",
            "成功工具调用必须形成可追溯 Evidence",
            "模型文字不能替代用户要求的 Artifact",
        ),
    )
    if chart_required:
        contract = contract.require_artifact("chart")
    if report_required:
        contract = contract.require_artifact("report", "pdf" if pdf_required else None)
    return contract
