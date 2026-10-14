"""执行前与完成校验共享的已验证派生数据集范围。"""

from __future__ import annotations

from packages.orchestration.analysis_requirements import (
    AnalysisRequirements,
    filter_arguments_match,
)
from packages.session.models import JsonObject
from packages.session.task_models import (
    EvidenceRecord,
    TaskDatasetBinding,
    ToolInvocation,
)


def is_filter_preserving_cleanup(arguments: JsonObject) -> bool:
    """仅接纳不改列值、不追加筛选的行清洗；具体参数仍由工具 schema 校验。"""
    operations = {"drop_nulls", "drop_duplicates", "sort", "exclude_row_indices"}
    return bool(operations.intersection(arguments)) and set(arguments) <= operations | {
        "dataset_ref"
    }


def verified_analysis_scope(
    requirements: AnalysisRequirements,
    run_id: str,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
    bindings: list[TaskDatasetBinding],
) -> set[str]:
    """仅沿本轮成功、证据一致且已登记的单父变换推进不可变版本范围。

    显式筛选以完整条件直接产生的总体为根，仅沿不改变条件的清洗边推进。
    没有显式筛选时允许合法清洗链；同项目的无关版本不是祖先。
    """
    scope = {requirements.dataset_ref} if requirements.dataset_ref else set()
    succeeded = {
        item.invocation_id: item
        for item in invocations
        if item.run_id == run_id
        and item.status == "succeeded"
        and item.tool_name == "transform_dataset"
        and any(
            record.run_id == run_id
            and record.invocation_id == item.invocation_id
            and record.result_hash == item.result_hash
            for record in evidence
        )
    }
    edges = [
        binding
        for binding in bindings
        if binding.run_id == run_id
        and binding.binding_kind == "derived"
        and binding.parent_ref
        and binding.dataset_ref != binding.parent_ref
        and (item := succeeded.get(binding.producing_invocation_id or "")) is not None
        and item.args.get("dataset_ref") == binding.parent_ref
    ]
    if requirements.filters:
        scope = {
            binding.dataset_ref
            for binding in edges
            if filter_arguments_match(
                requirements, succeeded[binding.producing_invocation_id or ""].args
            )
        }
        edges = [
            binding
            for binding in edges
            if is_filter_preserving_cleanup(
                succeeded[binding.producing_invocation_id or ""].args
            )
        ]
    while True:
        additions = {
            binding.dataset_ref for binding in edges if binding.parent_ref in scope
        }
        if additions.issubset(scope):
            return scope
        scope.update(additions)
