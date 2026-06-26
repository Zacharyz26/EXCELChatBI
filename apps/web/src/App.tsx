import { ChatPanel } from "@/components/ChatPanel";
import { ExcelUpload } from "@/components/ExcelUpload";
import { ReportPanel } from "@/components/ReportPanel";

/** 应用根组件：对话 + Excel 上传 + 报告面板（骨架布局）。 */
export default function App() {
  return (
    <div className="app">
      <h1>ChatBI 智能体</h1>
      <ExcelUpload />
      <ChatPanel />
      <ReportPanel />
    </div>
  );
}
