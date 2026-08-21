import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, formatApiError } from "../api/client";
import { APP_BRAND_LOGO_SRC, APP_BRAND_NAME } from "../app/brand";
import { useAuth } from "../auth/AuthProvider";

export function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      await auth.login(username, password);
      navigate("/", { replace: true });
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.code === "invalid_credentials"
          ? "账号或密码不对，请再检查一次。"
          : formatApiError(caught, `进入${APP_BRAND_NAME} 失败`),
      );
    }
  }

  return (
    <section className="auth-layout">
      <div className="auth-hero">
        <img className="auth-brand-logo" src={APP_BRAND_LOGO_SRC} alt={APP_BRAND_NAME} />
        <span className="eyebrow">{APP_BRAND_NAME}</span>
        <h1>{APP_BRAND_NAME}</h1>
        <p>把对话、模型、工具和自动化任务放进同一个工作台，关键操作可追踪、可审批、可恢复。</p>
      </div>
      <form className="auth-card" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="eyebrow">安全入口</span>
          <h2>登录工作台</h2>
          <p className="field-help">继续处理对话、计划任务、工具审批和系统配置。</p>
        </div>
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
            autoComplete="current-password"
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit">进入工作台</button>
      </form>
    </section>
  );
}
