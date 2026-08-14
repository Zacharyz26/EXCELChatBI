import { useCallback, useEffect, useMemo, useState } from "react";
import { getProjectLineage } from "@/api/client";
import type {
  LineageEdge,
  LineageGraphResponse,
  LineageNode,
  LineageNodeStatus,
  LineageNodeType,
  LineageRelation,
} from "@/types";

const STAGES: LineageNodeType[] = [
  "dataset",
  "analysis",
  "artifact",
  "evidence",
  "claim",
];
const STAGE_LABELS: Record<LineageNodeType, string> = {
  dataset: "数据集",
  analysis: "分析执行",
  artifact: "分析工件",
  evidence: "证据",
  claim: "结论",
};
const STATUS_LABELS: Record<LineageNodeStatus, string> = {
  active: "有效",
  deleted: "已删除",
  succeeded: "成功",
  failed: "失败",
  unknown: "未知",
  running: "运行中",
};
const RELATION_LABELS: Record<LineageRelation, string> = {
  derived_from: "派生",
  used_by: "输入",
  produced: "产出",
  profiled_as: "画像",
  included_in: "汇入",
  substantiates: "形成证据",
  supports: "支持结论",
};

export function LineagePanel({
  projectId,
  projectName,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  const [graph, setGraph] = useState<LineageGraphResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await getProjectLineage(projectId);
      setGraph(response);
      setSelectedId((current) => (
        current && response.nodes.some((node) => node.node_id === current)
          ? current
          : null
      ));
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const nodesByStage = useMemo(() => {
    const grouped = new Map<LineageNodeType, LineageNode[]>(
      STAGES.map((stage) => [stage, []]),
    );
    for (const node of graph?.nodes ?? []) {
      grouped.get(node.node_type)?.push(node);
    }
    return grouped;
  }, [graph]);
  const graphNodes = graph?.nodes ?? [];
  const selected = graphNodes.find((node) => node.node_id === selectedId) ?? null;
  const graphEdges = graph?.edges ?? [];
  const selectedEdges = graph && selected
    ? graphEdges.filter(
      (edge) => edge.source === selected.node_id || edge.target === selected.node_id,
    )
    : [];

  return (
    <div className="lineage-backdrop" role="presentation">
      <section
        className="lineage-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="lineage-title"
      >
        <header className="lineage-header">
          <div>
            <span>PROJECT LINEAGE</span>
            <h2 id="lineage-title">数据血缘</h2>
            <p>{projectName} · 从来源数据到可验证结论</p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭数据血缘">×</button>
        </header>

        <div className="lineage-summary">
          <div>
            <strong>{graph?.total_nodes ?? 0}</strong>
            <span>节点</span>
          </div>
          <div>
            <strong>{graph?.total_edges ?? 0}</strong>
            <span>关系</span>
          </div>
          <div className={`lineage-integrity lineage-integrity--${graph?.integrity_status ?? "unknown"}`}>
            <strong>{graph?.integrity_status === "ok" ? "完整" : "需检查"}</strong>
            <span>完整性</span>
          </div>
          <p>
            图指纹 {graph ? graph.graph_hash.slice(0, 12) : "—"}
            {graph?.truncated ? " · 当前为有界视图" : ""}
          </p>
          <button type="button" onClick={() => void load()} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>

        {error && <div className="lineage-message lineage-message--error">{error}</div>}
        {graph?.issues.map((issue) => (
          <div className="lineage-message lineage-message--error" key={issue.code}>
            完整性检查 {issue.code}：{issue.count} 项
          </div>
        ))}

        <div className="lineage-content">
          <div className="lineage-flow" aria-label="项目血缘阶段">
            {STAGES.map((stage, index) => {
              const nodes = nodesByStage.get(stage) ?? [];
              return (
                <section className={`lineage-stage lineage-stage--${stage}`} key={stage}>
                  <header>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <h3>{STAGE_LABELS[stage]}</h3>
                    <small>{nodes.length}</small>
                  </header>
                  <div>
                    {nodes.length === 0 ? (
                      <p className="lineage-stage__empty">暂无节点</p>
                    ) : nodes.map((node) => (
                      <button
                        type="button"
                        className={`lineage-node${selectedId === node.node_id ? " lineage-node--selected" : ""}`}
                        key={node.node_id}
                        onClick={() => setSelectedId(
                          selectedId === node.node_id ? null : node.node_id,
                        )}
                        aria-pressed={selectedId === node.node_id}
                      >
                        <span className={`lineage-node__status lineage-node__status--${node.status}`}>
                          {STATUS_LABELS[node.status]}
                        </span>
                        <strong>{node.label}</strong>
                        <small>{node.resource_ref.slice(0, 12)}</small>
                        <em>{edgeSummary(node, graphEdges)}</em>
                      </button>
                    ))}
                  </div>
                </section>
              );
            })}
          </div>

          {selected && (
            <aside className="lineage-detail" aria-label="血缘节点详情">
              <div className="lineage-detail__heading">
                <div>
                  <span>{STAGE_LABELS[selected.node_type]}</span>
                  <h3>{selected.label}</h3>
                </div>
                <button
                  type="button"
                  onClick={() => setSelectedId(null)}
                  aria-label="关闭节点详情"
                >×</button>
              </div>
              <dl>
                <div><dt>状态</dt><dd>{STATUS_LABELS[selected.status]}</dd></div>
                <div><dt>资源引用</dt><dd>{selected.resource_ref}</dd></div>
                <div><dt>创建时间</dt><dd>{formatDate(selected.created_at) || "未知"}</dd></div>
                <div><dt>运行</dt><dd>{selected.run_id ?? "不适用"}</dd></div>
              </dl>
              {safeFacts(selected).length > 0 && (
                <>
                  <h4>安全元数据</h4>
                  <dl>
                    {safeFacts(selected).map(([key, value]) => (
                      <div key={key}><dt>{key}</dt><dd>{value}</dd></div>
                    ))}
                  </dl>
                </>
              )}
              <h4>直接关系</h4>
              {selectedEdges.length === 0 ? (
                <p className="lineage-detail__empty">没有直接关系。</p>
              ) : (
                <ul>
                  {selectedEdges.map((edge) => (
                    <li key={`${edge.source}-${edge.target}-${edge.relation}`}>
                      <span>{edgeLabel(edge, selected.node_id)}</span>
                      <small>{otherEnd(edge, selected.node_id, graphNodes)}</small>
                    </li>
                  ))}
                </ul>
              )}
              <p className="lineage-detail__safety">
                此视图不返回工具参数、文件路径、数据样本或结果正文。
              </p>
            </aside>
          )}
        </div>
      </section>
    </div>
  );
}

function edgeSummary(node: LineageNode, edges: LineageEdge[]): string {
  const incoming = edges.filter((edge) => edge.target === node.node_id).length;
  const outgoing = edges.filter((edge) => edge.source === node.node_id).length;
  const parents = edges.filter(
    (edge) => edge.target === node.node_id && edge.relation === "derived_from",
  ).length;
  if (parents > 1) return `${parents} 个父版本 · ${outgoing} 出`;
  return `${incoming} 入 · ${outgoing} 出`;
}

function safeFacts(node: LineageNode): [string, string][] {
  const labels: Record<string, string> = {
    artifact_type: "工件类型",
    source_tool: "来源工具",
    tool_name: "执行工具",
    analysis_id: "逻辑分析",
    claim_kind: "结论类型",
    evidence_count: "证据数量",
    kind: "证据类型",
    derived: "派生数据",
    parent_count: "父版本数量",
  };
  return Object.entries(labels).flatMap(([key, label]) => {
    const value = node.metadata[key];
    if (value === null || value === undefined || value === "") return [];
    return [[label, typeof value === "boolean" ? (value ? "是" : "否") : String(value)]];
  });
}

function edgeLabel(edge: LineageEdge, selectedId: string): string {
  if (
    edge.relation === "derived_from"
    && edge.target === selectedId
    && edge.ordinal !== null
  ) {
    const role = edge.role === "primary"
      ? "主输入"
      : edge.role === "secondary"
        ? "次输入"
        : edge.role;
    return `父版本 ${edge.ordinal + 1}${role ? ` · ${role}` : ""}`;
  }
  return RELATION_LABELS[edge.relation];
}

function otherEnd(
  edge: LineageEdge,
  selectedId: string,
  nodes: LineageNode[],
): string {
  const nodeId = edge.source === selectedId ? edge.target : edge.source;
  const node = nodes.find((item) => item.node_id === nodeId);
  return node
    ? `${node.label} · ${node.resource_ref.slice(0, 12)}`
    : nodeId.replace(":", " · ").slice(0, 36);
}

function formatDate(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function messageOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : "血缘图读取失败，请重试。";
}
