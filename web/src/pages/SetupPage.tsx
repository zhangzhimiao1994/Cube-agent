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
      setError("Setup failed");
    }
  }

  return (
    <section>
      <h1>Initialize Agent Hub</h1>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Setup code
          <input
            aria-label="Setup code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
        <label>
          Username
          <input
            aria-label="Username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          Password
          <input
            aria-label="Password"
            value={password}
            type="password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit">Create admin</button>
      </form>
    </section>
  );
}
