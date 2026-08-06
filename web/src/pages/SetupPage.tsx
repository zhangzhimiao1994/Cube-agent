import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";

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
    } catch {
      setError("初始化失败");
    }
  }

  return (
    <section>
      <h1>初始化 Agent Hub</h1>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          初始化码
          <input
            aria-label="初始化码"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
        <label>
          用户名
          <input
            aria-label="用户名"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          密码
          <input
            aria-label="密码"
            value={password}
            type="password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit">创建管理员</button>
      </form>
    </section>
  );
}
