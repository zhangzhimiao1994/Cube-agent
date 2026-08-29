# Workspace Rules

- This repository is the active local continuation of CubeAgent/Agent Hub under `E:\code_x\mofang agent`.
- The production server remains the SSH host `prod-web-01`; the active release pointer is `/opt/agent-hub/current`.
- For runtime-affecting changes, deploy to `prod-web-01` and run a real feature-specific probe before pushing GitHub.
- Do not change public URL, port forwarding, secrets, provider credentials, or quota-consuming provider behavior without explicit user authorization.
- Keep `HANDOFF.md`, `task_plan.md`, `findings.md`, and `progress.md` local-only. Never commit secrets, virtual environments, caches, temporary probes, deployment packages, or local archives.
- Use test-driven development for features and bug fixes: add a focused regression, observe the expected failure, implement the smallest fix, then run focused and broader checks.
- After pushing GitHub, inspect the triggered run/check. If it fails, retrieve details, fix, verify, push, and repeat until green or a real external blocker is documented.
- If Docker, Docker Desktop, Docker Compose, or WSL2-backed containers are used, close Docker Desktop and shut down the WSL Docker backend after verification unless the user explicitly asks to keep them running.
- Before finishing Docker-related work, verify cleanup with `Get-Process '*docker*','vmmem*','wsl*'` and `wsl -l -v`; ask for approval if cleanup requires it.
- After every completed work slice, append a handoff entry containing current state, changes, verification, production details, remaining risks, and enough context to continue safely.
