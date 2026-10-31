"""执行前与完成校验共享的已验证派生数据集范围。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from packages.common.identifiers import dataset_reference_arguments
from packages.orchestration.analysis_requirements import (
    AnalysisRequirements,
    OperationRequirement,
    filter_arguments_match,
)
from packages.orchestration.evidence_facts import valid_count_projection
from packages.session.models import Artifact, JsonObject
from packages.session.task_models import (
    EvidenceRecord,
    TaskDatasetBinding,
    ToolInvocation,
)


def selected_analysis_dataset_refs(
    arguments: JsonObject, artifacts: list[Artifact]
) -> set[str] | None:
    """解析报告选择的持久化来源；缺失或无版本来源不能证明总体。"""
    wanted = arguments.get("analysis_ids")
    if (
        not isinstance(wanted, list)
        or not wanted
        or any(not isinstance(item, str) or not item for item in wanted)
    ):
        return None
    selected: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        analysis_id = next(
            (
                container["analysis_id"]
                for container in (artifact.params or {}, artifact.payload or {})
                if isinstance(container.get("analysis_id"), str) and container["analysis_id"]
            ),
            artifact.id,
        )
        if analysis_id in wanted:
            selected.setdefault(analysis_id, []).append(artifact)
    if set(selected) != set(wanted):
        return None
    sources = [artifact for group in selected.values() for artifact in group]
    if any(not artifact.dataset_ref for artifact in sources):
        return None
    return {str(artifact.dataset_ref) for artifact in sources}


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
    if getattr(requirements, "schema_version", 1) == 2:
        progress = operation_progress(requirements, run_id, invocations, evidence, bindings)
        return set(progress.scope) if progress.complete else set()
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
            if is_filter_preserving_cleanup(succeeded[binding.producing_invocation_id or ""].args)
        ]
    while True:
        additions = {binding.dataset_ref for binding in edges if binding.parent_ref in scope}
        if additions.issubset(scope):
            return scope
        scope.update(additions)


@dataclass(frozen=True, slots=True)
class OperationProgress:
    """Proof from successful invocations and immutable lineage, never plan labels."""

    required: tuple[str, ...]
    completed: tuple[str, ...]
    scope: frozenset[str]
    inputs: tuple[tuple[str, str], ...] = ()
    outputs: tuple[tuple[str, str], ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues and self.completed == self.required


def _argument_hash(arguments: JsonObject) -> str:
    # Same canonical JSON used by the persisted ToolInvocation contract.
    return hashlib.sha256(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _successful_record(
    invocation: ToolInvocation, run_id: str, evidence: list[EvidenceRecord]
) -> EvidenceRecord | None:
    if (
        invocation.run_id != run_id
        or invocation.status != "succeeded"
        or not invocation.result_hash
        or invocation.args_hash != _argument_hash(invocation.args)
    ):
        return None
    return next(
        (
            record
            for record in evidence
            if record.run_id == run_id
            and record.invocation_id == invocation.invocation_id
            and record.result_hash == invocation.result_hash
            and record.source.get("tool") == invocation.tool_name
        ),
        None,
    )


def _required_definition_error(
    operation: OperationRequirement,
    *,
    run_id: str,
    tool_name: str,
    arguments: JsonObject,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
) -> str | None:
    """Prove a named formula before executing it; model choices are not bindings.

    Reuse the immutable definition/compiled-invocation projection written by
    the existing Host. A knowledge citation, a later lookup, or an equally
    named formula compiled for a different dataset cannot authorize this call.
    The caller supplies only the invocation prefix, also during final replay.
    """
    key = operation.parameters.definition_key
    if key is None:
        return None
    identities: set[tuple[str, int, str]] = set()
    authorized: set[tuple[str, int, str]] = set()
    for invocation in invocations:
        if (
            invocation.tool_name != "domain_definition_lookup"
            or invocation.args.get("semantic_key") != key
            or invocation.args.get("dataset_ref") != arguments.get("dataset_ref")
        ):
            continue
        record = _successful_record(invocation, run_id, evidence)
        if record is None or record.source.get("dataset_ref") != arguments.get("dataset_ref"):
            continue
        resource = record.source.get("definition_resource")
        compiled = record.source.get("compiled_invocation")
        if not isinstance(resource, dict) or not isinstance(compiled, dict):
            continue
        definition_id = resource.get("definition_id")
        version = resource.get("definition_version")
        formula_hash = resource.get("formula_hash")
        if (
            resource.get("semantic_key") != key
            or not isinstance(definition_id, str)
            or not definition_id
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or not isinstance(formula_hash, str)
            or not formula_hash
            or resource.get("resource_uri") != f"chatbi://domain-definitions/{definition_id}"
            or not isinstance(resource.get("source_ref"), str)
            or compiled.get("definition_match") is not True
            or compiled.get("definition_id") != definition_id
            or compiled.get("definition_version") != version
            or isinstance(compiled.get("definition_version"), bool)
            or compiled.get("formula_hash") != formula_hash
            or compiled.get("tool_name") != tool_name
        ):
            continue
        identity = (definition_id, version, formula_hash)
        identities.add(identity)
        # The current task contract has no historical-version selection slot.
        # Explicit as_of lookups remain read-only exploration, not permission
        # for a model to select a formula by finding matching data arguments.
        if "as_of" not in invocation.args and compiled.get("arguments_hash") == _argument_hash(
            arguments
        ):
            authorized.add(identity)
    if len(identities) != 1 or authorized != identities:
        return "命名指标须匹配本轮默认时间解析的唯一受控定义及精确编译参数；不能自行选择历史版本。"
    return None


def _operation_arguments_equal(expected: JsonObject, arguments: JsonObject) -> bool:
    actual = {key: value for key, value in arguments.items() if key != "dataset_ref"}
    if "agg" in expected:
        # aggregate_preview explicitly separates display from computation.
        # A valid limit truncates rows, never the full-data total. Preserve the
        # existing schema bounds even when this proof runs after execution.
        if "limit" in actual:
            limit = actual.pop("limit")
            if type(limit) is not int or not 1 <= limit <= 100:
                return False
        if "sort" in actual:
            sort = actual.pop("sort")
            if not isinstance(sort, str) or sort not in {"value_desc", "value_asc", "group"}:
                return False
        if expected.get("agg") == "count" and actual.get("agg") == "count":
            # The tool's documented default counts non-null group values; it
            # cannot stand in for a different metric or generic record count.
            expected = dict(expected)
            expected.setdefault("value_col", expected.get("group_col"))
            actual.setdefault("value_col", actual.get("group_col"))
    if set(actual) != set(expected):
        return False
    for key, value in expected.items():
        if key in {"filters", "drop_nulls", "drop_duplicates"}:
            supplied = actual[key]
            # These parameters name unordered sets, not execution steps. Keep
            # cardinality as well as equality; duplicates cannot hide omissions.
            if (
                not isinstance(value, list)
                or not isinstance(supplied, list)
                or len(value) != len(supplied)
                or any(not any(_same_argument(item, other) for other in supplied) for item in value)
            ):
                return False
        elif not _same_argument(actual[key], value):
            return False
    return True


def _same_argument(left: object, right: object) -> bool:
    """JSON booleans are not numeric literals, even though Python True == 1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_argument(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_argument(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def operation_progress(
    requirements: AnalysisRequirements,
    run_id: str,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
    bindings: list[TaskDatasetBinding],
) -> OperationProgress:
    """Prove each v2 operation in order with one invocation and its real version.

    Partial progress is usable for the next operation's admission, but does not
    certify final delivery. Failed attempts do not advance this cursor. An
    actual successful out-of-contract write remains a violation even when a
    later invocation produces the expected numbers.
    """
    requirements.validate_integrity()
    operations = requirements.active_operations()
    required = tuple(operation.operation_id for operation in operations)
    current = requirements.dataset_ref
    completed: list[str] = []
    inputs: list[tuple[str, str]] = []
    outputs: list[tuple[str, str]] = []
    issues: list[str] = []
    if operations and current is None:
        issues.append("操作缺少已绑定数据集。")
    for invocation_index, invocation in enumerate(invocations):
        if invocation.run_id != run_id or invocation.status != "succeeded":
            continue
        if invocation.tool_name not in {"transform_dataset", "aggregate_preview"}:
            continue
        operation = operations[len(completed)] if len(completed) < len(operations) else None
        tool = (
            "aggregate_preview"
            if operation and operation.kind == "aggregate"
            else ("transform_dataset")
        )
        optional_transform = not operations and invocation.tool_name == "transform_dataset"
        if invocation.tool_name != tool or (operation is None and not optional_transform):
            if invocation.tool_name == "transform_dataset" and operations:
                issues.append("成功变换不属于当前有序操作。")
            elif invocation.tool_name == "aggregate_preview" and operation is not None:
                issues.append("成功汇总先于当前必须执行的有序操作。")
            # Optional exploratory aggregates do not satisfy another operation.
            continue
        expected = requirements.operation_arguments(operation.operation_id) if operation else None
        if invocation.args.get("dataset_ref") != current or (
            expected is not None and not _operation_arguments_equal(expected, invocation.args)
        ):
            issues.append("成功操作的参数、顺序或输入版本不符合冻结任务。")
            continue
        if operation is not None:
            definition_error = _required_definition_error(
                operation,
                run_id=run_id,
                tool_name=invocation.tool_name,
                arguments=invocation.args,
                invocations=invocations[:invocation_index],
                evidence=evidence,
            )
            if definition_error:
                issues.append(definition_error)
                continue
        record = _successful_record(invocation, run_id, evidence)
        if record is None or not valid_count_projection(
            record.summary,
            tool=invocation.tool_name,
            run_id=run_id,
            invocation_id=invocation.invocation_id,
            result_hash=invocation.result_hash or "",
        ):
            issues.append("操作缺少对应成功调用的有效事实投影。")
            continue
        projection = record.summary["fact_projection"]
        if projection.get("arguments") != invocation.args or projection.get("input_ref") != current:
            issues.append("操作证据与实际参数或输入版本不一致。")
            continue
        next_ref = current
        if invocation.tool_name == "transform_dataset":
            children = {
                item.dataset_ref
                for item in bindings
                if item.run_id == run_id
                and item.binding_kind == "derived"
                and item.producing_invocation_id == invocation.invocation_id
                and item.parent_ref == current
                and item.dataset_ref != current
                and item.dataset_ref == projection.get("output_ref")
            }
            if len(children) != 1:
                issues.append("变换没有唯一已登记、证据一致的衍生版本。")
                continue
            next_ref = next(iter(children))
        elif projection.get("output_ref") != current:
            issues.append("汇总证据引用了错误的数据集版本。")
            continue
        assert current is not None and next_ref is not None
        if operation is not None:
            completed.append(operation.operation_id)
            inputs.append((operation.operation_id, current))
            outputs.append((operation.operation_id, next_ref))
        current = next_ref
    return OperationProgress(
        required=required,
        completed=tuple(completed),
        scope=frozenset({current}) if current else frozenset(),
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        issues=tuple(dict.fromkeys(issues)),
    )


def operation_admission_error(
    requirements: AnalysisRequirements,
    *,
    run_id: str,
    tool_name: str,
    arguments: JsonObject,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
    bindings: list[TaskDatasetBinding],
    source_artifacts: list[Artifact] | None = None,
) -> str | None:
    """Shared v2 serial/parallel admission; never reparse the model's intent."""
    from packages.orchestration.knowledge_intent import knowledge_query_allowed

    try:
        requirements.validate_integrity()
        if requirements.schema_version != 2:
            return "操作准入需要明确受支持的任务契约版本。"
        if requirements.needs_clarification():
            return "任务仍有未解决的分析槽位，请先完成对应澄清。"
        progress = operation_progress(requirements, run_id, invocations, evidence, bindings)
        operations = requirements.active_operations()
    except ValueError as exc:
        return f"冻结分析契约无效：{exc}"
    if progress.issues:
        return progress.issues[0]
    order_error = _required_order_error(
        requirements, progress, run_id, tool_name, arguments, invocations, evidence
    )
    if order_error:
        return order_error
    if tool_name in {"kb_search", "domain_definition_lookup"}:
        query = arguments.get("query", arguments.get("semantic_key"))
        if not knowledge_query_allowed(
            str(query) if query is not None else "",
            requirements.knowledge_queries,
            requirements.knowledge_prohibitions,
        ):
            return "知识查询不符合当前冻结的对象禁令或改口范围。"
    refs = {str(ref) for _, ref in dataset_reference_arguments(arguments)}
    if tool_name == "generate_report" and (requirements.dataset_ref or operations):
        selected = selected_analysis_dataset_refs(arguments, source_artifacts or [])
        if selected is None:
            return "报告所选分析缺少可验证的数据集版本。"
        refs.update(selected)
    operation = operations[len(progress.completed)] if not progress.complete else None
    if tool_name == "transform_dataset":
        if not operations:
            key_by_kind = {
                "filter": "filters",
                "drop_nulls": "drop_nulls",
                "drop_duplicates": "drop_duplicates",
            }
            if any(
                (
                    key_by_kind.get(item.kind) in arguments
                    or ("exclude_row_indices" in arguments and item.kind in key_by_kind)
                )
                and item.disposition
                in {"prohibited", "deferred", "described", "absent", "cancelled"}
                for item in requirements.operations
            ):
                return "本次数据变换未获当前执行授权。"
        elif operation is None or operation.kind == "aggregate":
            return "当前没有待执行的数据变换义务，不能新增或重复变换。"
        elif not _operation_arguments_equal(
            requirements.operation_arguments(operation.operation_id), arguments
        ):
            return "变换必须逐步执行下一项已确认操作，不得合并、增删条件或改变列范围。"
    if (
        tool_name in {"aggregate_preview", "dimension_contribution"}
        and any(
            item.kind == "aggregate" and item.disposition != "current"
            for item in requirements.operations
        )
        and not any(item.kind == "aggregate" for item in operations)
    ):
        return "汇总在本次请求中并非当前执行操作。"
    if tool_name == "aggregate_preview" and operations:
        if operation is not None and operation.kind != "aggregate":
            return "必要数据操作尚未完成，不能提前汇总中间版本。"
        if operation is None and any(item.kind == "aggregate" for item in operations):
            return "尚未到达该汇总步骤，或该步骤已经完成。"
        if operation is not None and not _operation_arguments_equal(
            requirements.operation_arguments(operation.operation_id), arguments
        ):
            return "汇总的分组、指标或方法与已确认任务不一致。"
        if operation is not None:
            definition_error = _required_definition_error(
                operation,
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                invocations=invocations,
                evidence=evidence,
            )
            if definition_error:
                return definition_error
    if (
        refs
        and operations
        and not progress.complete
        and tool_name
        not in {
            "get_data_profile",
            "transform_dataset",
            "aggregate_preview",
            "domain_definition_lookup",
            "kb_search",
        }
    ):
        return "必要数据操作尚未完成，不能从中间版本交付后续分析。"
    if progress.scope and refs and not refs.issubset(progress.scope):
        return "工具引用的数据集不是当前操作链的已验证版本。"
    if requirements.total and tool_name in {"aggregate_preview", "dimension_contribution"}:
        if (
            arguments.get("value_col") != requirements.total_column
            or arguments.get("agg", arguments.get("method", "sum")) != "sum"
        ):
            return "总计必须使用已确认的指标与sum，不能更换度量或聚合口径。"
    return None


def _required_order_error(
    requirements: AnalysisRequirements,
    progress: OperationProgress,
    run_id: str,
    tool_name: str,
    arguments: JsonObject,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
) -> str | None:
    """Use the same frozen source order as the plan, with actual prefix proof.

    A later successful query cannot retroactively authorize an earlier data
    operation. The complete verifier replays this gate for every success, so
    final plan labels and the set of successful tools are not sufficient.
    """
    from packages.orchestration.knowledge_intent import knowledge_query_matches

    operations = requirements.active_operations()
    queries = requirements.knowledge_queries
    if not queries:
        return None
    if any(not query.source_spans for query in queries):
        return "必要知识查询缺少可核验的原文顺序依据。"
    completed_queries = {
        index
        for index, query in enumerate(queries)
        if any(
            item.tool_name in {"kb_search", "domain_definition_lookup"}
            and _successful_record(item, run_id, evidence) is not None
            and knowledge_query_matches(
                str(item.args.get("query", item.args.get("semantic_key", ""))), query
            )
            for item in invocations
        )
    }
    position: int | None = None
    if tool_name in {"kb_search", "domain_definition_lookup"}:
        value = str(arguments.get("query", arguments.get("semantic_key", "")))
        matches = [
            index for index, query in enumerate(queries) if knowledge_query_matches(value, query)
        ]
        if len(matches) > 1:
            return "本次查询不能同时替代多个冻结对象的独立查询步骤。"
        if len(matches) == 1:
            index = matches[0]
            if index in completed_queries:
                # Definition lookup may legitimately be followed by KB search
                # for the same object. This read-only follow-up cannot satisfy
                # any other obligation; the first success is still replayed
                # with all of its original prerequisites at completion.
                return None
            position = min(start for start, _ in queries[index].source_spans)
    elif tool_name in {"transform_dataset", "aggregate_preview"}:
        if len(progress.completed) < len(operations):
            position = operations[len(progress.completed)].span[0]
    if position is None:
        return None
    if any(
        item.span[0] < position and item.operation_id not in progress.completed
        for item in operations
    ):
        return "该步骤的前置数据操作尚未由本轮成功证据和版本血缘证明。"
    if any(
        min(start for start, _ in query.source_spans) < position and index not in completed_queries
        for index, query in enumerate(queries)
    ):
        return "该步骤的前置知识查询尚未形成精确对象匹配的本轮成功 Evidence。"
    return None


def required_knowledge_issues(
    requirements: AnalysisRequirements,
    run_id: str,
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
) -> tuple[str, ...]:
    """A plan label or unrelated statistics cannot satisfy a required query."""
    from packages.orchestration.knowledge_intent import knowledge_query_matches

    requirements.validate_integrity()
    missing = []
    for query in requirements.knowledge_queries:
        if not any(
            invocation.tool_name in {"kb_search", "domain_definition_lookup"}
            and _successful_record(invocation, run_id, evidence) is not None
            and knowledge_query_matches(
                str(invocation.args.get("query", invocation.args.get("semantic_key", ""))), query
            )
            for invocation in invocations
        ):
            missing.append("必需知识查询缺少同一任务中、精确对象匹配的成功工具Evidence。")
    return tuple(missing)
