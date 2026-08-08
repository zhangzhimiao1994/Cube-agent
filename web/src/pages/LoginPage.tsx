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
      setError(
        caught instanceof ApiError && caught.status === 401
          ? "Invalid username or password"
          : "Login failed",
      );
    }
  }

  return (
    <section className="auth-layout">
      <div className="auth-hero">
        <span className="eyebrow">Agent Hub</span>
        <h1>Login</h1>
        <p>
          Manage model routing, agent roles, skills, memory and audit trails from one protected
          operations console.
        </p>
      </div>
      <form className="auth-card" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="eyebrow">Session</span>
          <h2>Welcome back</h2>
        </div>
        <label>
          Username
          <input
            aria-label="Username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
        </label>
        <label>
          Password
          <input
            aria-label="Password"
            value={password}
            type="password"
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit">Login</button>
      </form>
    </section>
  );
}
