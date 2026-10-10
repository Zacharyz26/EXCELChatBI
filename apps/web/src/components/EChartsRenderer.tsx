// ECharts 渲染器：后端下发 ECharts JSON，前端渲染（设计文档 5.1）
import { useEffect, useRef } from "react";
import { BarChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";

echarts.use([
  BarChart, CanvasRenderer, GridComponent,
  LineChart, PieChart, ScatterChart, TitleComponent, TooltipComponent,
]);

interface Props {
  option: Record<string, unknown>;
  chartId: string; // 绑定底层数据，后续支持点击追问
}

function chartAccessibleLabel(option: Record<string, unknown>, chartId: string): string {
  const rawTitles = Array.isArray(option.title) ? option.title : [option.title];
  const title = rawTitles
    .filter((value): value is Record<string, unknown> => !!value && typeof value === "object")
    .map((value) => value.text)
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  const rawSeries = Array.isArray(option.series) ? option.series : [];
  const seriesName = rawSeries
    .filter((value): value is Record<string, unknown> => !!value && typeof value === "object")
    .map((value) => value.name)
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  const summary = [title?.trim(), seriesName?.trim()].filter(Boolean).join("，")
    || "数据可视化";
  return `${summary}（图表 ${chartId}）`;
}

export function EChartsRenderer({ option, chartId }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const ariaLabel = chartAccessibleLabel(option, chartId);

  useEffect(() => {
    if (!ref.current) return;
    const inst = echarts.init(ref.current);
    inst.setOption(option as EChartsOption);
    const onResize = () => inst.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      inst.dispose();
    };
  }, [option]);

  return (
    <div
      aria-label={ariaLabel}
      data-chart-id={chartId}
      ref={ref}
      role="img"
      style={{ width: "100%", height: 400 }}
    />
  );
}
