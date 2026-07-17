// 助手正文的 Markdown 渲染：实时流与历史回放共用同一渲染路径。
// react-markdown 不走 innerHTML，流式期间对不完整 Markdown 容忍良好（逐增量重解析）。
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Props {
  content: string;
}

export function MarkdownText({ content }: Props) {
  return (
    <div className="md-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // 模型输出里的链接一律新开页，避免跳走丢失对话状态
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer">{children}</a>
          ),
          // 表格包一层横向滚动容器，长表不撑破气泡
          table: ({ children }) => (
            <div className="md-table-scroll"><table>{children}</table></div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
