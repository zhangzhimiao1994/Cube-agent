import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
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
      setError(caught instanceof ApiError && caught.status === 401 ? "用户名或密码错误" : "登录失败");
    }
  }

  return (
    <section>
      <h1>登录</h1>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          用户名
          <input
            aria-label="用户名"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
        </label>
        <label>
          密码
          <input
            aria-label="密码"
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
