"""v2.5 阶段 3E：项目级 Dataset/Analysis/Artifact/Claim 血缘 API。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from packages.governance.permissions import Principal
from packages.session.lineage import LineageAccessDenied, LineageStore
from packages.session.store import SessionStore

from apps.api.auth import current_principal_dep
from apps.api.deps import session_store_dep
from apps.api.schemas import (
    LineageEdgeResponse,
    LineageGraphResponse,
    LineageIssueResponse,
    LineageNodeResponse,
)

router = APIRouter(prefix="/projects/{project_id}/lineage", tags=["lineage"])


@router.get("", response_model=LineageGraphResponse)
def get_project_lineage(
    project_id: str,
    conversation_id: str | None = None,
    max_nodes: Annotated[int, Query(ge=1, le=2_000)] = 500,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> LineageGraphResponse:
    """读取项目或单个对话的有界血缘图，越权范围统一按不存在处理。"""
    try:
        graph = LineageStore(store).build_graph(
            project_id=project_id,
            conversation_id=conversation_id,
            principal=principal,
            max_nodes=max_nodes,
        )
    except LineageAccessDenied as exc:
        raise HTTPException(status_code=404, detail="血缘范围不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LineageGraphResponse(
        project_id=graph.project_id,
        nodes=[
            LineageNodeResponse(
                node_id=node.node_id,
                node_type=node.node_type,
                resource_ref=node.resource_ref,
                label=node.label,
                status=node.status,
                conversation_id=node.conversation_id,
                run_id=node.run_id,
                metadata=node.metadata,
                created_at=node.created_at,
            )
            for node in graph.nodes
        ],
        edges=[
            LineageEdgeResponse(
                source=edge.source,
                target=edge.target,
                relation=edge.relation,
            )
            for edge in graph.edges
        ],
        graph_hash=graph.graph_hash,
        integrity_status=graph.integrity_status,
        issues=[
            LineageIssueResponse(code=issue.code, count=issue.count)
            for issue in graph.issues
        ],
        total_nodes=graph.total_nodes,
        total_edges=graph.total_edges,
        truncated=graph.truncated,
    )
