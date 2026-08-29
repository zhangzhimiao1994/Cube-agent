# Workspace Rules

- This repository is the active Mofang Agent / Cube-agent harness continuation.
- Production validation hostnames, public URLs, port-forwarding details, and GitHub remote targets are local operational details. Check the local handoff/context and `git remote -v` before deploying or pushing, but do not commit concrete public endpoints or credentials.
- For every harness/refactor batch, finish local implementation and verification first, deploy the change to the configured production validation server, run a real feature-specific probe there, push to GitHub only after the server probe is clean, inspect the triggered GitHub run/check, and continue the next batch only after the server and GitHub checks are green or a real blocker is documented.
- Do not change public URL, port forwarding, secrets, provider credentials, or quota-consuming provider behavior without explicit user authorization.
- Keep `HANDOFF.md`, `task_plan.md`, `findings.md`, and `progress.md` local-only. Never commit secrets, virtual environments, caches, temporary probes, deployment packages, or local archives.
- Use test-driven development for features and bug fixes: add a focused regression, observe the expected failure, implement the smallest fix, then run focused and broader checks.
- After pushing GitHub, inspect the triggered run/check. If it fails, retrieve details, fix, verify, push, and repeat until green or a real external blocker is documented.
- If Docker, Docker Desktop, Docker Compose, or WSL2-backed containers are used, close Docker Desktop and shut down the WSL Docker backend after verification unless the user explicitly asks to keep them running.
- Before finishing Docker-related work, verify cleanup with `Get-Process '*docker*','vmmem*','wsl*'` and `wsl -l -v`; ask for approval if cleanup requires it.
- After every completed work slice, append a handoff entry containing current state, changes, verification, production details, remaining risks, and enough context to continue safely.
