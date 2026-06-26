// ECharts 渲染器：后端下发 ECharts JSON，前端渲染（设计文档 5.1）
import type { EChartsOption } from "echarts";

interface Props {
  option: EChartsOption;
  chartId: string; // 绑定底层数据，支持点击追问
}

export function EChartsRenderer(_props: Props) {
  // TODO: useRef + echarts.init + setOption；点击事件回传 chartId 用于追问
  return <div className="echarts" />;
}
