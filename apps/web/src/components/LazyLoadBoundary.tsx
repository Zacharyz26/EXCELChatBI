import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label: string;
  onDismiss?: () => void;
}

interface State {
  error: Error | null;
}

/** Keep a rejected lazy chunk local and offer a fresh-document recovery path. */
export class LazyLoadBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    // React reports the component stack in development; the fallback remains user-actionable.
  }

  private reload = () => {
    const confirmed = window.confirm(
      "刷新会丢失未发送的聊天草稿和面板中未保存的输入。是否继续？",
    );
    if (!confirmed) return;
    window.location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="lazy-load-error" role="alert">
        <strong>{this.props.label}加载失败</strong>
        <p>
          页面资源可能已更新或网络暂时中断。刷新会重新获取当前版本并恢复此区域，
          但未发送的聊天草稿和面板中未保存的输入会丢失。
        </p>
        <div className="lazy-load-error__actions">
          {this.props.onDismiss && (
            <button type="button" onClick={this.props.onDismiss}>
              关闭
            </button>
          )}
          <button type="button" onClick={this.reload}>
            刷新并重试
          </button>
        </div>
      </div>
    );
  }
}
