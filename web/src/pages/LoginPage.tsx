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
          : formatApiError(caught, "进入魔方agent失败"),
      );
    }
  }

  return (
    <section className="auth-layout">
      <div className="auth-hero">
        <img className="auth-brand-logo" src={APP_BRAND_LOGO_SRC} alt={APP_BRAND_NAME} />
        <span className="eyebrow">{APP_BRAND_NAME}</span>
        <h1>魔方agent</h1>
        <p>从一个入口发起对话、调度 Agent、管理模型和工具权限。每一次执行都有过程记录，关键操作先确认再落地。</p>
      </div>
      <form className="auth-card" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="eyebrow">欢迎回来</span>
          <h2>进入魔方agent</h2>
          <p className="field-help">登录后继续上次的对话、任务编排和系统配置。</p>
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
