import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { formatApiError } from "../api/client";
import { APP_BRAND_LOGO_SRC, APP_BRAND_NAME } from "../app/brand";
import { useAuth } from "../auth/AuthProvider";

const INSTALL_STEPS = [
  ["安装依赖", "安装系统包、Python 运行时、Node 构建工具、Redis 和 PostgreSQL。"],
  ["部署版本", "把代码复制到 /opt/agent-hub/releases，并更新 current 指向。"],
  ["执行迁移", "API 接收流量前先升级数据库结构。"],
  ["启动服务", "启动 Caddy、API、worker、LiteLLM 和隔离 Skill 服务。"],
  ["创建管理员", "使用安装器打印的一次性设置码创建第一个管理员账号。"],
] as const;

export function SetupPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      await auth.setup(code, username, password);
      navigate("/", { replace: true });
    } catch (caught) {
      setError(formatApiError(caught, "初始化失败"));
    }
  }

  return (
    <section className="auth-layout setup-layout">
      <div className="auth-hero">
        <img className="auth-brand-logo" src={APP_BRAND_LOGO_SRC} alt={APP_BRAND_NAME} />
        <span className="eyebrow">{APP_BRAND_NAME}</span>
        <h1>初始化 {APP_BRAND_NAME}</h1>
        <p>创建第一个管理员后，就可以接入模型、通道、Skill 和 OpenClaw 工具。</p>
        <p>设置码只用于首次启用，使用后立即失效。</p>
        <div className="setup-timeline" aria-label="安装流程">
          {INSTALL_STEPS.map(([title, detail], index) => (
            <article key={title} className="timeline-step">
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <h2>{title}</h2>
                <p>{detail}</p>
              </div>
            </article>
          ))}
        </div>
      </div>
      <form className="auth-card" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="eyebrow">首次启用</span>
          <h2>创建第一个账号</h2>
        </div>
        <label>
          一次性设置码
          <input
            aria-label="Setup code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="one-time-code"
          />
        </label>
        <label>
          用户名
          <input
            aria-label="Username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
        </label>
        <label>
          密码
          <input
            aria-label="Password"
            value={password}
            type="password"
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="new-password"
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit">创建管理员</button>
      </form>
    </section>
  );
}
