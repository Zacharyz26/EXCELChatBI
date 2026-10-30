"""从既有工具成功事务记录只读核对交付基数。"""

from __future__ import annotations

from packages.session.models import Artifact, JsonObject
from packages.session.task_models import EvidenceRecord, ToolInvocation


def verified_chart_deliveries(
    *,
    run_id: str | None,
    evidence: list[EvidenceRecord],
    invocations: list[ToolInvocation],
    artifacts: list[Artifact],
) -> list[JsonObject] | None:
    """逐成员交叉核对并保留来源 ID，不把请求中的数量当成交付。

    任一成员损坏使集合数量未知，不能直接少算；重复 Evidence 不增计
    工件。此处不持久化派生计数，也不建立第二份业务事实来源。
    """
    if run_id is None:
        return None
    output: dict[str, JsonObject] = {}
    scope: tuple[object, object] | None = None
    for call in invocations:
        if call.run_id != run_id or call.tool_name != "gen_chart" or call.status != "succeeded":
            continue
        records = [item for item in evidence if item.invocation_id == call.invocation_id]
        matches = [item for item in artifacts if item.id == call.artifact_id]
        if not records or len(matches) != 1:
            return None
        artifact = matches[0]
        args = call.args
        encoding = args.get("encoding")
        if (
            artifact.type != "chart"
            or artifact.source_tool != "gen_chart"
            or not isinstance(artifact.params, dict)
            or {
                key: value
                for key, value in artifact.params.items()
                if key not in {"analysis_id", "data_version_hash"}
            }
            != args
            or not isinstance(artifact.payload, dict)
            or not isinstance(artifact.payload.get("option"), dict)
            or not artifact.payload["option"]
            or not isinstance(encoding, dict)
            or not isinstance(encoding.get("x"), str)
            or not encoding["x"].strip()
            or not isinstance(args.get("dataset_ref"), str)
            or artifact.dataset_ref != args["dataset_ref"]
        ):
            return None
        for record in records:
            context = record.summary.get("numeric_context")
            if (
                record.run_id != run_id
                or record.source.get("tool") != "gen_chart"
                or record.source.get("dataset_ref", args["dataset_ref"]) != args["dataset_ref"]
                or record.result_hash != call.result_hash
                or not record.result_hash
                or record.artifact_id != artifact.id
                or not isinstance(context, dict)
                or not context.get("data_version_hash")
                or artifact.params.get("data_version_hash", context["data_version_hash"])
                != context["data_version_hash"]
                or {key: value for key, value in context.items() if key != "data_version_hash"}
                != args
            ):
                return None
            member_scope = (args["dataset_ref"], context["data_version_hash"])
            if scope is not None and member_scope != scope:
                return None
            scope = member_scope
        member: JsonObject = {
            "artifact_id": artifact.id,
            "invocation_id": call.invocation_id,
            "result_hash": call.result_hash,
            "evidence_id": records[0].evidence_id,
            "dataset_ref": args["dataset_ref"],
            "dataset_version": scope[1] if scope else None,
            "dimension": encoding["x"],
        }
        if artifact.id in output and output[artifact.id] != member:
            return None
        output[artifact.id] = member
    return list(output.values()) or None
