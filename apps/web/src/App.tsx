import { useEffect, useState, type FormEvent } from "react";
import {
  getAuthMode,
  hasApiToken,
  listProjects,
  setApiToken,
} from "@/api/client";
import { ChatWorkspace } from "@/components/ChatWorkspace";

/** 对话式数据分析 Agent 唯一入口（阶段4：经典五页已按能力清单核对后下线）。 */
export default function App() {
  const [state, setState] = useState<"loading" | "login" | "ready" | "error">(
    "loading",
  );
  const [token, setToken] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void getAuthMode()
      .then((mode) => {
        setState(mode === "disabled" || hasApiToken() ? "ready" : "login");
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "无法读取认证配置");
        setState("error");
      });
  }, []);

  async function authenticate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const clean = token.trim();
    if (!clean) return;
    setError("");
    setApiToken(clean);
    try {
      await listProjects();
    } catch (reason) {
      setApiToken("");
      setError(reason instanceof Error ? reason.message : "令牌验证失败");
      return;
    }
    setToken("");
    setState("ready");
  }

  if (state === "ready") return <ChatWorkspace />;
  if (state === "loading") {
    return <div className="auth-shell"><p>正在连接 ChatBI…</p></div>;
  }
  if (state === "error") {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <h1>ChatBI</h1>
          <p role="alert">{error}</p>
          <button type="button" onClick={() => window.location.reload()}>重试</button>
        </div>
      </div>
    );
  }
  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={(event) => void authenticate(event)}>
        <div className="conversation-brand__mark">BI</div>
        <h1>连接 ChatBI</h1>
        <p>请输入管理员分配的访问令牌。令牌仅保存在当前浏览器会话中。</p>
        <label htmlFor="api-token">访问令牌</label>
        <input
          id="api-token"
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          autoComplete="current-password"
          autoFocus
        />
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={!token.trim()}>进入工作区</button>
      </form>
    </div>
  );
}
