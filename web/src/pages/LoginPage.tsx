import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, formatApiError } from "../api/client";
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
          ? "用户名或密码错误"
          : formatApiError(caught, "登录失败"),
      );
    }
  }

  return (
    <section className="auth-layout">
      <div className="auth-hero">
        <span className="eyebrow">Agent Hub</span>
        <h1>登录控制台</h1>
        <p>
          在一个受保护的生产控制台里管理模型路由、Agent 角色、Skill、记忆、通道和审计日志。
        </p>
      </div>
      <form className="auth-card" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="eyebrow">Session</span>
          <h2>账号登录</h2>
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
        <button type="submit">登录</button>
      </form>
    </section>
  );
}
