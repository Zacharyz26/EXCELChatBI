// ECharts 渲染器：后端下发 ECharts JSON，前端渲染（设计文档 5.1）
import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";

interface Props {
  option: Record<string, unknown>;
  chartId: string; // 绑定底层数据，后续支持点击追问
}

export function EChartsRenderer({ option, chartId }: Props) {
  const ref = useRef<HTMLDivElement>(null);

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

  return <div data-chart-id={chartId} ref={ref} style={{ width: "100%", height: 400 }} />;
}
