import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";

const INSTALL_STEPS = [
  ["Install packages", "System packages, Python runtime, Node build toolchain, Redis and PostgreSQL."],
  ["Deploy release", "Code is copied into /opt/agent-hub/releases and current is updated."],
  ["Run migrations", "Database schema is upgraded before API services accept traffic."],
  ["Start services", "Caddy, API, worker, LiteLLM and isolated skill services are started."],
  ["Create administrator account", "Use the printed one-time setup code to create the first admin."],
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
    } catch {
      setError("Setup failed");
    }
  }

  return (
    <section className="auth-layout setup-layout">
      <div className="auth-hero">
        <span className="eyebrow">Secure first-run setup</span>
        <h1>Initialize Agent Hub</h1>
        <p>Use the one-time code printed by the installer.</p>
        <p>The code opens the first administrator account and then becomes unusable.</p>
        <div className="setup-timeline" aria-label="Installation flow">
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
          <span className="eyebrow">Administrator</span>
          <h2>Create first account</h2>
        </div>
        <label>
          Setup code
          <input
            aria-label="Setup code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="one-time-code"
          />
        </label>
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
            autoComplete="new-password"
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit">Create admin</button>
      </form>
    </section>
  );
}
