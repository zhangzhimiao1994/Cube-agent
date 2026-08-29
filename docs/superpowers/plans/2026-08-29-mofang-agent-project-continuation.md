# Mofang Agent Project Continuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `E:\code_x\mofang agent` as the active CubeAgent continuation workspace and close the remaining locally and externally verifiable P3 work.

**Architecture:** Preserve the current Git repository and handoff as the baseline, while excluding regenerable machine-local artifacts. Audit the existing React application page-by-page, add regression coverage before each concrete UI correction, then verify and deliver through the established `prod-web-01` server-first workflow and GitHub checks.

**Tech Stack:** PowerShell, Git, Python 3.12, FastAPI, React 18, TypeScript, Vite, Vitest, Playwright, systemd/Caddy on `prod-web-01`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-29-mofang-agent-project-continuation-design.md`

## Global Constraints

- Preserve `E:\code_x\agent-hub`; all implementation work occurs in `E:\code_x\mofang agent`.
- Copy `.git`, source, tests, documentation, configuration, and `HANDOFF.md`; exclude `.venv`, `node_modules`, caches, build outputs, `.tmp`, and `.local-archives`.
- Production remains `prod-web-01` with active release pointer `/opt/agent-hub/current`.
- Do not change public URL, forwarding, ports, secrets, or provider configuration without explicit authorization.
- Runtime-affecting changes deploy and pass a real server probe before GitHub push.
- If Docker/Compose/WSL containers are used, stop Docker Desktop and the WSL Docker backend and verify cleanup before handoff.
- Never commit or push `HANDOFF.md`, secrets, virtual environments, caches, temporary probes, or local archives.

---

### Task 1: Create the continuation workspace

**Files:**
- Create: `E:\code_x\mofang agent\` from the approved baseline
- Create/Modify: `E:\code_x\mofang agent\AGENTS.md`
- Create: `E:\code_x\mofang agent\task_plan.md`
- Create: `E:\code_x\mofang agent\findings.md`
- Create: `E:\code_x\mofang agent\progress.md`
- Preserve: `E:\code_x\mofang agent\HANDOFF.md`

**Interfaces:**
- Consumes: local repository at commit `164ca1a` or later and the local-only handoff.
- Produces: an independent working directory with complete Git history and persistent project state.

- [ ] **Step 1: Inventory excluded directories and destination state**

Run `Get-ChildItem -Force` and size checks for `.venv`, `node_modules`, caches, `.tmp`, and `.local-archives`; verify the destination does not already exist.

- [ ] **Step 2: Copy the approved project content**

Use one PowerShell-native copy operation with explicit exclusions. Do not delete or alter the source directory.

- [ ] **Step 3: Add the root project instructions and planning files**

Record the machine-wide Docker/WSL cleanup rule, server-first delivery rule, `prod-web-01` target, GitHub CI rule, and local handoff requirement in `AGENTS.md`. Initialize the three planning files with the objective, phases, discoveries, and actions already completed.

- [ ] **Step 4: Verify the copy**

Compare tracked file lists and `HEAD`, confirm `HANDOFF.md` exists, and confirm excluded directories are absent.

- [ ] **Step 5: Commit only project-root bootstrap files that belong in Git**

Keep planning files and `HANDOFF.md` local-only where required; update `.gitignore` if necessary. Commit `AGENTS.md` and plan/spec documents only when they are intended repository documentation.

### Task 2: Reconcile local and GitHub state

**Files:**
- Modify if needed: `E:\code_x\mofang agent\.git\config`
- Update: `E:\code_x\mofang agent\findings.md`
- Update: `E:\code_x\mofang agent\progress.md`

**Interfaces:**
- Consumes: GitHub repository `zhangzhimiao1994/CubeAgent`, local `main`, and existing recovery tags.
- Produces: a documented commit comparison and a safe current remote without changing source code.

- [ ] **Step 1: Inspect remotes and URL rewrite configuration**

Record the existing `origin`/`mutilagent` URLs and the machine-level HTTPS-to-SSH rewrite that caused the earlier read failure.

- [ ] **Step 2: Fetch current CubeAgent main safely**

Use an explicit reachable transport without exposing credentials. If network/sandbox access blocks the fetch, request the required approval and record the exact blocker.

- [ ] **Step 3: Compare histories**

Run merge-base, ahead/behind, and changed-file comparisons. Do not merge or reset until the result proves which side is authoritative.

- [ ] **Step 4: Reconcile without losing local work**

Fast-forward when possible. If histories diverge, preserve both with a recovery ref and integrate only after reviewing the diff.

### Task 3: Audit the remaining frontend surface

**Files:**
- Inspect: `web/src/pages/*.tsx`
- Inspect: `web/src/app/AppShell.tsx`
- Inspect: `web/src/styles.css`
- Test: `web/src/pages/*.test.tsx`
- Update: `findings.md`

**Interfaces:**
- Consumes: existing responsive layout rules, page tests, navigation structure, and handoff backlog.
- Produces: a concrete issue ledger with route, viewport, reproduction, expected behavior, and owning selector/component.

- [ ] **Step 1: Define the rendered smoke flow**

Use: `app loads -> authenticate/setup state renders -> each reachable module page renders -> primary controls remain contained at desktop and mobile widths without console errors`.

- [ ] **Step 2: Start the existing frontend without changing dependencies**

Use the repository package scripts. Prefer the Browser plugin because it is available; if invocation fails, record the exact failure before using the existing Playwright setup.

- [ ] **Step 3: Audit desktop and mobile routes**

Check clipping, overflow, dense form controls, table/filter layout, ambiguous text, empty states, framework overlays, and console warnings. Save screenshots outside the repository.

- [ ] **Step 4: Record only reproducible gaps**

For each issue, name the production CSS/component change that would make a regression test pass. Do not create speculative redesign work.

### Task 4: Fix each verified UI gap with TDD

**Files:**
- Modify: the smallest affected `web/src/pages/<Page>.tsx` or `web/src/app/AppShell.tsx`
- Modify: `web/src/styles.css`
- Test: the matching `web/src/pages/<Page>.test.tsx` or `web/src/app/AppShell.test.tsx`

**Interfaces:**
- Consumes: Task 3 issue ledger.
- Produces: minimal tested layout/text corrections with unchanged API contracts.

- [ ] **Step 1: Add one focused regression test**

Assert the concrete accessible label, semantic class/state, or rendered structure required for the first verified issue.

- [ ] **Step 2: Run the focused test and verify RED**

Run `npm.cmd test -- --run <matching-test-file> -t "<test name>"` from `web`; confirm failure is caused by the missing behavior.

- [ ] **Step 3: Implement the smallest correction**

Change only the owning component/CSS rule. Preserve established component and navigation patterns.

- [ ] **Step 4: Run the focused test and verify GREEN**

Repeat the exact focused command and confirm it passes.

- [ ] **Step 5: Repeat RED/GREEN for each remaining verified issue**

Keep each cycle independent and update `progress.md` with the failing and passing evidence.

- [ ] **Step 6: Render the corrected flows**

Reload the same routes and viewports, check page identity, nonblank content, overlay absence, console health, screenshots, and at least one relevant interaction.

### Task 5: Run local quality gates

**Files:**
- Update: `progress.md`
- Update: `HANDOFF.md`

**Interfaces:**
- Consumes: the completed frontend slice.
- Produces: reproducible local verification evidence ready for deployment.

- [ ] **Step 1: Run affected tests**

Run each changed test file in isolation.

- [ ] **Step 2: Run the full frontend test suite**

Run `npm.cmd test -- --run` from `web` and record exact file/test counts.

- [ ] **Step 3: Run static and production checks**

Run `npm.cmd run lint`, `npm.cmd run build`, and `git diff --check`.

- [ ] **Step 4: Review the diff**

Remove unrelated formatting churn and confirm no generated artifacts, secrets, handoff files, screenshots, or temporary probes are staged.

### Task 6: Deploy and probe `prod-web-01`

**Files:**
- Create locally then remove/ignore: deployment archive generated from Git-tracked files
- Update locally: `HANDOFF.md`, `progress.md`

**Interfaces:**
- Consumes: locally verified tracked changes.
- Produces: a backed-up production release with feature-specific proof and clean temporary state.

- [ ] **Step 1: Confirm deployment authorization and connectivity**

Use the existing SSH host alias `prod-web-01`; never print secrets.

- [ ] **Step 2: Create recovery material and tracked-file package**

Archive only intended tracked paths, compute SHA256, and exclude local-only files.

- [ ] **Step 3: Back up and deploy**

On `prod-web-01`, identify `/opt/agent-hub/current`, create timestamped backup/archive paths, copy the active release, overlay the increment, build frontend assets if changed, switch the symlink, and restart only required services.

- [ ] **Step 4: Verify service and real behavior**

Check `agent-hub-api`, `agent-hub-worker`, and Caddy as applicable; verify `/health`; inspect served current assets for new stable markers; exercise the corrected route behavior.

- [ ] **Step 5: Clean server temporary files**

Remove only the exact uploaded archive and temporary probe/deploy scripts after verifying their resolved paths are under `/tmp`.

### Task 7: Deliver GitHub state and close the handoff

**Files:**
- Update local-only: `HANDOFF.md`
- Update local-only: `progress.md`, `findings.md`, `task_plan.md`

**Interfaces:**
- Consumes: successful production verification and reconciled GitHub history.
- Produces: committed/pushed source, green CI, and complete future-work context.

- [ ] **Step 1: Commit intended tracked changes**

Stage explicit source/test/docs files only and commit with a focused message.

- [ ] **Step 2: Create recovery bundle and tag**

Preserve the remote pre-push main in a local bundle and timestamped recovery tag.

- [ ] **Step 3: Push safely**

Fetch current main and push with lease protection according to the project SOP.

- [ ] **Step 4: Monitor GitHub Actions**

List the triggered CubeAgent run, wait for completion, retrieve failure details if needed, fix locally, redeploy only when runtime behavior changes, and repeat until green or a real external blocker is documented.

- [ ] **Step 5: Perform Docker/WSL cleanup checks**

If Docker was used, stop Docker Desktop/WSL Docker backend. In all cases record `Get-Process '*docker*','vmmem*','wsl*'` and `wsl -l -v` results relevant to cleanup.

- [ ] **Step 6: Finalize handoff**

Append objective, changed files, local checks, production backup/release/archive, real probes, commit SHA, recovery material, Actions run ID, remaining external-only gaps, and next action to local `HANDOFF.md`.
