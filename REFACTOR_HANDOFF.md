# Agent Hub 重构交接文档

更新时间：2026-08-30

用途：这份文档单独记录后续重构方向。当前项目仍以“先稳定已有功能”为优先；如果后续新开会话或新项目做重构，应从这里接手。

## 2026-08-30 当前实现记录：运行报错细化

本轮在现有系统上继续增强运行失败诊断，目标是让运行中断不再只显示一条泛化 `reason`。

已完成：

- 后端新增结构化失败诊断：`error_summary`、`error_stage`、`error_category`、`error_code`、`retryable`、`status_code`、`suggested_action`。
- `runtime.failed` 合约已允许携带安全 `payload`。
- `RunRepository.persist_event()` 会为 `runtime.failed`、`step.failed`、`tool.failed` 自动补齐诊断 payload，覆盖运行时主动上报失败和 service 捕获异常两类路径。
- AutoGen / Crew / Hybrid / UnavailableRuntime 的失败事件已接入诊断 payload。
- admin 日志中心的 mode error 会展示错误码、阶段、分类、是否可重试、状态码和建议动作。
- Web 对话失败气泡、运行过程详情、运行详情页新增结构化失败诊断展示。
- 同步修复一个 Skill 上传版本问题：包式 Skill 的管理层内容哈希改为按归一化文件内容计算，避免 ZIP/TAR 容器元数据变化导致“同内容重复上传”误判成新版本；运行沙箱执行仍使用真实包字节哈希校验。

已验证：

- `pytest tests/unit/runtime/test_failure_reason.py tests/unit/runs/test_repository_public_payload.py tests/contracts/test_runtime_contract.py tests/api/test_admin_resources.py -q`：238 passed。
- `pytest tests/unit/runtime/test_failure_reason.py tests/unit/runs/test_failure_reason.py tests/unit/runs/test_repository_public_payload.py tests/contracts/test_runtime_contract.py tests/api/test_admin_resources.py -q`：226 passed（追加细分分类前的首轮覆盖）。
- `pytest tests/unit/runtime/test_hybrid.py tests/unit/runtime/crew/test_adapter_failure_reason.py tests/unit/runs/test_observer.py tests/unit/runs/test_terminal_hooks.py -q`：20 passed。
- `ruff check` 覆盖本轮改动后端文件：passed。
- `mypy` 覆盖本轮核心后端文件：passed。
- `npm run lint`：passed。
- `npm test -- --run src/pages/OperationalPages.test.tsx`：72 passed。
- `npm run build`：passed，生成 `web/dist/assets/index-EjK5oq9c.js`。

生产状态：

- 已清理 `prod-web-01` 旧 release：保留 current 和最新 3 个，磁盘从 98% 降到 58%，部署后为 64%。
- 已部署到 `prod-web-01`：`/opt/agent-hub/releases/20260830-204000-runtime-failure-diagnostics`，`/opt/agent-hub/current` 已指向该 release。
- 服务状态：`agent-hub-api`、`agent-hub-worker`、`caddy` 均为 active。
- 生产健康检查：`curl http://127.0.0.1:8000/health` 返回 `{"status":"ok"}`。
- 生产专项探针：在服务器导入 `runtime_failure_diagnostic_from_reason("model gateway failed: model transport failed (status=401)")`，返回 `error_code=model.provider_auth_failed`、`error_category=authentication`、`retryable=False`。
- 前端生产 bundle 探针：`web/dist/assets/index-EjK5oq9c.js` 包含“失败诊断”。

Git 状态：

- 已提交并推送到 `zhangzhimiao1994/CubeAgent.git` 的 `codex/mofang-continuation` 分支，最新 HEAD 为 `9f759193b2f7e507bc2d06baceb7c7f51562a64c`。
- 相关提交：
  - `18fb1b4 Add structured runtime failure diagnostics`
  - `127f1fe Expand runtime failure categories`
  - `9f75919 Localize runtime failure diagnostics`
- GitHub combined status 和 commit workflow runs 对 `9f75919` 均返回空列表，表示该仓库/分支当前没有可读取的触发检查。

未完成/注意：

- `tests/integration/runtime/test_hybrid_runtime.py` 在本机因测试 PostgreSQL 端口未就绪超时，9 个 setup errors；不是业务断言失败。需要有本地集成测试数据库或 Docker 测试环境后再跑。

## 当前目标口径

目标不是做出 CowAgent 风格的页面雏形，而是达到 CowAgent 这类 Agent Harness 的可用层级：

- Web 控制台可管理；
- 多通道能接入并能真实回复；
- 主 Agent 能规划、调度、追踪、恢复；
- 多模型/中转站配置后能真实调用；
- Skill、MCP、插件可安装、授权、执行；
- 文件、图片、压缩包等附件能进入任务上下文；
- 长期记忆、知识、Hermes 学习能实际影响后续行为；
- 日志、审计、运行过程能排障；
- 新服务器一键部署后可直接登录使用。

## 当前判断

不建议在现有代码上继续直接堆 vibe coding、OpenClaw、更多通道和更多工具能力。当前系统已经包含主 Agent、子 Agent、模型网关、工作流、通道、Skill、MCP、Hermes、附件、权限、日志、安装部署等多条链路，但部分链路边界仍不够清晰。

建议先做“小范围架构边界重构”，不是推倒重写。

## 建议模块边界

### 1. Conversation

职责：

- 多轮对话；
- 会话列表；
- 会话删除；
- 会话上下文读取；
- handoff / “按照原思路开启新对话”；
- 附件生命周期；
- 对话内文字交互式确认。

验收口径：

- 新建对话才出现初始模式选择；
- 旧对话进入后必须恢复历史；
- 新结果不能覆盖旧消息；
- 对话过程以主回答为主，关键动作以一行过程条展示，点击过程条看详情；
- 非初始设置类选择，走文字交互，不走弹窗；
- 系统级危险操作，例如删除会话，仍走弹窗确认。

### 2. Main Agent / Orchestrator

职责：

- 判断自动、直连、派单、讨论、混合；
- 根据任务选择角色；
- 根据角色和任务选择模型；
- 当角色缺失时，按配置询问用户是否临时创建子 Agent；
- 任务完成后询问是否将临时 Agent 永久化；
- 控制成本：能少用子 Agent 就少用，但跨领域任务允许多 Agent；
- 需要用户确认时，走对话文字交互，不走弹窗。

关键规则：

- 主 Agent 单独配置模型/API，不复用普通子 Agent 配置；
- 派单、讨论、混合中，子 Agent 使用哪个模型应由主 Agent 根据模型能力和任务要求判断；
- 直连模式只需要选择模型，不需要选择子 Agent；
- 自动模式不能静默回退直连，必须说明原因并让用户确认或修正；
- 临时 Agent 生成能力用于解决“当前没有合适角色”的问题。

### 3. Runtime

职责：

- direct；
- dispatch；
- discuss；
- hybrid；
- 执行状态恢复；
- 队列消费；
- 错误中断时输出断点前内容和明确错误。

验收口径：

- 每种模式都要有可运行的真实链路；
- 不允许只生成 artifact 而不返回正常文字交互；
- 过程事件要包含：谁执行、调用哪个模型、收到什么任务、做了什么、产出什么、讨论什么、主 Agent 如何决策；
- 失败时要能从 UI 日志和服务日志定位到模型、通道、Skill、MCP 或 Runtime 边界。

### 4. Model Gateway

职责：

- 官方模型；
- AI 中转站；
- 自定义 Base URL；
- `/v1` 自动兼容；
- OpenAI-compatible、Anthropic messages、Claude Code 类中转格式；
- 模型可用性测试；
- 模型错误日志；
- 并发/限流配置建议。

验收口径：

- 模型注册、修改、删除闭环；
- 主 Agent 模型配置也要注册、修改、删除闭环；
- 配置失败必须落日志，并在 UI 展示脱敏详情；
- 中转站不能因为 Base URL 是否带 `/v1` 造成误判。

### 5. Tools / Skills / MCP / Plugins

职责：

- Skill 安装；
- Skill 审批；
- MCP 连接；
- 插件管理；
- 工具安全边界；
- 通道内通过文本指令引用工具能力。

验收口径：

- Skill 上传包支持 zip、tar、tar.gz 等常见压缩格式；
- 压缩包不能自动当代码审查，需要由用户任务意图决定；
- Skill 应由主 Agent 安装/审核后进入能力池，再按任务下发给子 Agent；
- 普通管理员可以安装 Skill/插件，但不能删除/禁用关键能力；
- MCP 和插件也要有真实调用验证，不做 mock-only。

### 6. Channels

职责：

- 飞书；
- 钉钉；
- 企业微信；
- 微信客服/公众号类；
- Telegram；
- Slack；
- QQ；
- Generic webhook。

验收口径：

- 每个通道都有清晰配置指引；
- 需要外部回调的 URL 必须使用部署时填写的外部访问地址；
- 通道文本交互规则：
  - `/` 选择运行模式；
  - `@` 引用插件；
  - `/#` 引用 MCP；
  - `&` 引用 Skill；
  - 多个工具/Skill/MCP 时需要定义组合语法并返回提示；
- 通道能接收附件时，应进入统一附件服务。

### 7. Observability / Logs / Trace

职责：

- 模型错误；
- 模式运行错误；
- Agent 角色错误；
- 通道连接错误；
- Skill/MCP/插件错误；
- 系统主要功能错误；
- 审计日志；
- 对话运行过程 trace。

验收口径：

- 正常日志默认不收集或少收集，默认等级 warning；
- 错误日志必须能看到具体原因、HTTP 状态、服务商、Base URL、模型名、request id；
- 运行过程展示不能显示 `model.started`、`artifact.created` 这类低价值事件；
- 应转换为用户能理解的一句话过程条。

### 8. Web UI

职责：

- 6 个大类导航；
- 每个大类内部用色块/卡片划分模块；
- 手机端适配；
- 对话 UI；
- 模型配置；
- Agent 配置；
- 工作流配置；
- 通道配置；
- Skill/MCP/插件；
- Hermes；
- 日志；
- 用户和权限。

验收口径：

- 对话页主区域只显示用户和 Agent 的交互；
- 内部过程折叠成一行，可点开详情；
- 附件、handoff、新建对话、模式选择应在对话流中自然出现；
- 系统级危险操作，如删除会话/删除模型/删除用户，仍然使用弹窗确认。

### 9. Hermes

职责：

- 从每次对话提取可学习经验；
- 按时间和会话 ID 列表展示；
- 支持批量确认/忽略；
- 学习内容经确认后影响主 Agent 调度、角色选择、模型选择、派单策略；
- 派单效果也进入 Hermes 学习闭环。

验收口径：

- Hermes 不能只是推荐页；
- 必须能看到“这次对话学到了什么、为什么建议沉淀、确认后影响什么”。

### 10. Deployment

职责：

- 一键部署；
- 新服务器检测；
- 国内镜像 fallback；
- native 直装；
- docker/compose；
- 外部访问地址；
- Caddy；
- systemd；
- 数据库迁移；
- 静态文件权限。

验收口径：

- 安装最后要求输入外部访问地址；
- 打印所有需要填到平台后台的回调地址；
- 不能依赖 root home 下的 Python；
- 不能因 CRLF、权限、release 缺失、uv/litellm extra 缺失导致启动失败；
- 更新策略应尽量增量，避免每次上传完整 release 和大 `.venv`。

## CowAgent 可参考点

基于 CowAgent README / 发布说明，重点参考以下能力，而不是照搬实现：

- Agent Harness 分层：Channels → Agent Core → Memory/Knowledge/Tools/Skills → Models → Reply；
- 一键安装、启动、更新；
- Web 控制台统一管理聊天、模型、通道、技能；
- 多渠道 7×24 运行；
- 模型切换和中转兼容；
- Skill 多来源安装；
- 文件、图片、目录上传；
- 工具调用失败刷新后仍可见；
- 长上下文控制；
- 知识库和长期记忆自动沉淀。

## 后续新增大功能建议顺序

1. 先稳定现有功能链路。
2. 做模块边界重构。
3. 接 OpenClaw：作为系统级功能开关和受控长会话/session provider，不揉进 main Agent 核心。
4. 做 Vibe Coding：集成在 Conversation 对话能力中，依赖 Attachments、Runtime、ModelGateway、Trace、Git/代码审查能力和权限控制，不做 standalone 模块或 workflow preset。

## 新项目/新会话接手时的第一步

1. 读取 `HANDOFF.md` 获取当前实际状态。
2. 读取本文件确认重构目标。
3. 跑本地测试，确认当前 main 是否健康。
4. 先盘点 UI 上“有按钮但未闭环”的功能。
5. 按模块逐个拆，而不是一次性大改。

## 2026-08-13 P3 Feature Boundary Update

- Vibe Coding 后续不作为单独系统模块，也不作为 workflow 预设；它应集成在对话能力中，由 Conversation 入口承载，并复用 Attachments、Runtime、Model Gateway、Trace、Git/代码审查能力和权限控制。
- OpenClaw 目标是长时间调用、随时操作电脑，因此不能只按一次性短调用 Tool 处理。
- OpenClaw 后续应作为系统级功能开关管理，默认关闭，由管理员显式开启。
- OpenClaw 开启项至少要包含：允许操作范围、会话超时、人工确认策略、审计级别、紧急停止行为。
- OpenClaw 底层应是受控长会话/session provider，具备 start/pause/resume/stop/audit/permission/timeout/emergency-stop；对 Agent 暴露时可以是 tool-call 风格能力，但生命周期和电脑控制状态不能塞进 main Agent 核心。

## 2026-08-30 Phase18 Task5 Frontend File Delivery Handoff

- Scope: frontend display for generated run artifacts with download metadata.
- Changed: `web/src/api/client.ts` now preserves artifact `filename`, `mime_type`, `size_bytes`, `sha256`, and `download_url` on both run artifact lists and event-embedded artifacts.
- Changed: added shared `web/src/components/ArtifactFileCard.tsx`; `RunsPage` renders downloadable artifact cards in the main chat and process drawer, and `RunDetailPage` renders the same download entry in the artifact list.
- Changed: `web/src/styles.css` contains compact file-card styling with desktop-first layout and natural mobile wrapping.
- Tests: added Vitest coverage in `web/src/pages/OperationalPages.test.tsx` for main chat download href, process drawer download href, and run detail download href/size display.
- Verification: `npm test -- OperationalPages.test.tsx --run` passed with 70 tests; `npm run lint` passed via `tsc -p tsconfig.json --noEmit`.
- Notes: no Docker/WSL work was used for this task. Other agents had concurrent backend/admin changes in the worktree; this task did not modify or revert them.

## 2026-08-30 Phase20 CrewAI Timeout Resilience

- Scope: fix `CrewAI step timed out: step=quality_reviewer_step actor=quality_reviewer` style failures so the location is diagnosable and large review/final prompts are less likely to time out.
- Changed: `src/agent_hub/runtime/failure_reason.py` classifies CrewAI step timeouts as `crew.step_timeout`, extracts `step_id` and `actor`, and marks them retryable.
- Changed: `src/agent_hub/runtime/defaults.py` now reserves more timeout budget for post-product/review roles and final synthesis than for producer steps, capped at 600 seconds.
- Changed: `src/agent_hub/runtime/crew/adapter.py` now emits structured diagnostics on `STEP_FAILED`, uses bounded `artifact_review_packet` payloads for reviewer/final source artifacts, and soft-fails reviewer timeouts as `review_status=timeout_skipped` instead of failing the whole dispatch.
- Tests: added/updated unit coverage in `tests/unit/runtime/test_failure_reason.py`, `tests/unit/runtime/test_configured_runtime.py`, and `tests/unit/runtime/crew/test_adapter_failure_reason.py`.
- Verification: `.venv\Scripts\python.exe -m pytest tests\unit\runtime\test_failure_reason.py tests\unit\runtime\test_configured_runtime.py tests\unit\runtime\crew\test_adapter_failure_reason.py -q` passed with 70 tests.
- Verification: `.venv\Scripts\python.exe -m ruff check src tests` passed.
- Verification: `git diff --check` passed with only the existing Git CRLF-to-LF warning for `src/agent_hub/runtime/crew/adapter.py`.
- Feishu identity note: WS submissions are currently isolated by derived actor UUID from `channel + tenant_external_id + sender_external_id`, but this does not resolve through `agent_hub_users.feishu_open_id`; user management therefore will not show a Feishu-bound user unless a persistent bind/unbind and channel identity resolver are added.
- Remaining TODO: implement Feishu identity binding as a follow-up: persistent lookup by Feishu `open_id`, explicit unbound-user fallback policy, user-management bind/unbind API, and Web UI display/edit controls.
