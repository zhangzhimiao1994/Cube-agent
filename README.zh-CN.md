# Cube Agent（魔方 Agent）

面向团队和私有部署场景的多 Agent 工作台：统一管理模型、项目工作区、会话、角色调度、Skill、插件、MCP、隔离执行环境、计划任务、Hermes 记忆与学习，以及完整的运行审计。

项目内部 Python 包名仍为 `agent_hub`；面向用户的产品名为 **Cube Agent / 魔方 Agent**。

[English README](README.md)

## 产品定位

Cube Agent 不是单一聊天页面，也不是允许模型任意下载和执行代码的开放式运行器。它把 Agent 工作流拆成可管理、可审批、可审计的几个层次：

- **交互层**：Web 工作台、项目与会话管理、附件、历史问题搜索和外部通道。
- **决策层**：主 Agent 根据任务选择直接回答、角色分派、多角色讨论或混合执行。
- **能力层**：模型池、Skill、插件、MCP、OpenClaw 和多媒体提供商。
- **执行层**：版本化能力环境、systemd 或 Docker 隔离后端、资源与权限约束。
- **记忆层**：Hermes 学习候选、分层长期记忆、来源追踪、审批和撤销。
- **治理层**：租户与用户隔离、权限审批、审计日志、运行详情和故障恢复。

系统适合需要自行掌握数据、模型入口和执行权限的团队。外部模型、通道、插件适配器和安全测试工具仍需由部署者提供合法凭证及可用运行环境。

## 当前已实现能力

### 项目、会话与运行

- 项目拥有租户隔离的共享工作区；同一项目可创建多个会话，会话不会自行改变项目或工作区归属。
- 支持新建、重命名、归档和恢复会话，并可从历史会话继续或建立参考关系。
- 每个用户问题生成可跳转检查点；服务端支持跨会话全历史问题搜索、项目/归档筛选、游标分页和精确锚点跳转。
- Agent 运行期间的新消息可排队；队列项可以编辑、取消或用于改变当前执行方向。
- 长输出、代码块、文件和运行产物使用有边界的预览区，可展开、复制、下载或跳转到来源动作。
- 项目生成能力已覆盖小型、中型、大型和超大型任务路径；实际质量仍取决于模型、可用工具、工作区内容和预算。

### 主 Agent 与多 Agent 调度

| 模式 | 行为 |
|---|---|
| `auto` | 主 Agent 结合任务、模型、角色和策略选择执行方式 |
| `direct` | 由选定模型直接完成任务 |
| `dispatch` | 将任务分派给一个或多个角色执行 |
| `discuss` | 组织多角色讨论并汇总结论 |
| `hybrid` | 结合分派与讨论流程 |

运行过程会记录角色调度、模型调用、工具生命周期、文件产物、失败诊断和恢复动作。子 Agent 在工作台中以中文名称和职责摘要展示；这些名称用于阅读，不改变后端角色身份。

### 模型与多媒体

- 普通模型用于对话、推理、工具调用、结构化输出和代码任务。
- 多媒体模型单独配置，并按 `image_generation`、`video_generation`、`audio_generation` 等能力标签路由。
- 可配置 OpenAI、DeepSeek、Anthropic、Moonshot/Kimi、Qwen/DashScope、MiniMax 和兼容 OpenAI/Anthropic 协议的服务。
- 当前仓库内置的真实多媒体执行客户端是 MiniMax/Hailuo 文生视频；其他预设只有在对应执行客户端和凭证均已配置后才可实际生成。
- LiteLLM 作为部署内模型网关；新安装不会自动获得任何外部模型额度或密钥。

### Skill、插件、MCP 与能力发现

这三类能力使用不同的治理路径：

- **Skill**：上传 `.zip`、`.tar`、`.tar.gz` 或 `.tgz`，经过解包扫描、隔离、权限审阅和批准后启用。支持多 Skill 包、版本激活和普通附件转 Skill 的明确操作。
- **团队 Skill Tap**：可同步受信仓库或导入离线 ZIP 快照。提交 SHA 与归档 SHA-256 必须匹配；每次同步形成不可变修订，全部候选批准后才能原子激活，并可整体回滚。
- **插件**：支持 manifest、HTTP JSON、本地命令等受控适配方式，以及签名、凭证、域名、生命周期和健康状态管理。
- **MCP**：独立配置 transport、命令或 URL、允许工具、可执行文件白名单、域名白名单和超时。
- **能力发现与安装**：主 Agent 可在运行中识别缺失能力并生成安装或配置方案。用户确认的是当前 `plan_id`；过期或被替换的方案会被拒绝。

能力安装器**不会任意从互联网下载并执行未知插件**。可信目录中的能力可以被规划、审批、安装、健康检查和回滚；缺少可执行文件、凭证、适配器或隔离后端时，系统会保持不可用状态，而不是显示一个虚假的“已启用”占位项。

当运行时缺失但存在明确恢复路径时，原任务会进入 `waiting_approval`，界面给出“安装运行时”“配置凭证”“启用能力”或“重启适配器”等动作。批准并验证成功后继续原任务；模型失败、内部错误和无法安全恢复的故障不会伪装成可安装问题。

### 隔离执行环境

能力环境按版本保存，可执行部署、烟雾检查、原子切换、回滚、配额清理和引用保护。当前实现支持两个执行后端：

| 后端 | 条件 | 隔离方式 |
|---|---|---|
| `systemd` | 原生 Linux 安装且 Skill broker 可用 | `DynamicUser`、只读系统、私有网络、资源限制、受控工作区绑定 |
| `docker` | Docker daemon 和 `agent-hub-skill-runner:latest` 均可用 | 临时只读容器、降权用户、丢弃 capabilities、`no-new-privileges`、资源与网络限制 |

原生 Linux 部署中，API 和 worker 以非特权用户运行；需要创建隔离单元的操作通过 socket 激活的最小权限 broker 完成。包路径、内容哈希、调用者、超时、输出上限和工作区绑定在 broker 边界再次校验。

Windows 可以通过本机浏览器选择项目目录，也可以作为远程桌面/OpenClaw 适配端；当前生产级 Skill 隔离后端仍是 Linux systemd 或 Docker，不应把 Windows 文件选择器误解为 Windows 原生沙箱。

### Hermes 学习与记忆

Hermes 是经验与记忆层，不是在线训练模型。

- 任务完成后可生成带来源证据的偏好、事实或规则候选。
- 候选默认不进入长期召回；确认后才提升为锁定、带作用域的长期记忆，拒绝或删除不会继续召回。
- 记忆分为 `working`、`episodic` 和 `core`，并按用户/租户、项目和会话作用域隔离。
- 运行时只召回与当前主体、项目和问题相关的有限记忆；记忆内容不能授予权限、切换模式或绕过审批。
- 学习台账支持状态、类别和记忆层筛选，显示来源任务、来源会话、审批状态，并可确认、拒绝、删除或遗忘。
- 敏感值会被拒绝或脱敏；用户记忆和 Hermes 候选按 actor 隔离，未知旧作用域默认关闭访问。

### 计划任务、通道与 OpenClaw

- 计划任务支持一次性执行、cron 周期、时区、误点策略、预算和工作流绑定。
- 对话中的计划需求先形成方案，确认后才创建持久任务；调度器不能绕过模型、工具或权限审批。
- 控制台提供飞书、钉钉、企业微信、微信、Telegram、Slack、QQ 和自定义 Webhook 配置入口。飞书具备完整的 WebSocket/Webhook 接入实现；其他通道是否可用取决于对应适配器和平台配置。
- OpenClaw 用于受控的 `server_command`、`desktop_action`、`screen_read` 和 `file_read`。支持 `ask`、`read_only`、`auto_review`、`trusted_auto` 权限模式，以及命令/路径白名单、远程适配器能力声明和审计。

### 日志与审计

- 日志中心按审计、模型、模式、功能、Agent 和通道分类，支持搜索、筛选、排序和 JSON 导出。
- `run.submit` 记录提交者、租户、会话、请求/最终模式、模型或角色选择、附件数量、消息摘要与哈希。
- 插件安装、Skill 审批、能力环境切换、Hermes 记忆变更、OpenClaw 和恢复操作均保留审计线索。
- 运行详情只公开安全摘要；原始密钥、隐藏推理和未经筛选的工具负载不应进入前端事件流。

## 架构

```text
浏览器 / 飞书等通道
          |
          v
Caddy -> React + TypeScript Web 控制台
          |
          v
FastAPI API ---- PostgreSQL（配置、会话、运行、审计、记忆）
    |  \
    |   +------ Redis（运行协调）
    |   +------ LiteLLM（模型网关）
    |
    v
后台 Worker -> 主 Agent / AutoGen / CrewAI 运行适配器
    |
    +------ Skill / 插件 / MCP / OpenClaw
    +------ systemd Skill broker 或 Docker 隔离后端
```

主要技术栈：Python 3.12、FastAPI、SQLAlchemy/asyncpg、PostgreSQL、Redis、Alembic、React 19、TypeScript、Vite、TanStack Query、LiteLLM、Caddy，以及 AutoGen/CrewAI 运行适配器。

## 快速开始

### 原生 Linux 安装（推荐）

准备一台干净的 Linux 服务器，最低建议 1 GiB 内存、2 GiB 可用磁盘，并允许访问所需软件源。

```bash
git clone https://github.com/zhangzhimiao1994/Cube-agent.git
cd Cube-agent
sudo bash install.sh --mode auto --yes
```

`--mode auto` 在检测到 systemd 和已识别的 apt/dnf 系发行版时选择原生部署，否则选择 Docker。发行版版本校验发生在模式选择之后，因此不受支持的旧版本系统应显式使用 `--mode docker`。已适配的原生发行版包括 Ubuntu 22.04/24.04、Debian 12/13、Rocky Linux 9 和 AlmaLinux 9。

网络受限环境可以启用镜像回退：

```bash
sudo env AGENT_HUB_MIRROR_MODE=auto bash install.sh --mode auto --yes
```

安装完成后，终端会输出 `/setup` 地址和限时初始化码。访问 `/setup` 创建首个超级管理员。安装器会保留已有的 `/etc/agent-hub/secrets.env` 和 `/var/lib/agent-hub`；重复执行进入修复/升级路径，不会重新生成并覆盖现有密钥。

### 首次配置顺序

1. 在“模型配置”中添加至少一个可用普通模型并验证连接。
2. 配置主 Agent 的默认模型、决策策略和 Hermes 策略。
3. 创建项目并选择项目工作区，再在项目中创建会话。
4. 在“执行环境”确认 systemd 或 Docker 后端真实可用。
5. 按需添加 Skill、插件、MCP、通道、多媒体模型和计划任务。
6. 先用低风险任务验证模型调用、文件产物、审批和审计，再开放高权限能力。

没有配置模型时，系统界面仍可进入，但不能完成需要模型推理的任务。

## 生产部署

### 原生 systemd

原生模式安装并管理 API、worker、LiteLLM、Skill broker socket/service、PostgreSQL、Redis 和 Caddy。API 默认绑定 `127.0.0.1`，由 Caddy 提供公开入口。

绑定已有 HTTPS 证书：

```bash
sudo env AGENT_HUB_PUBLIC_URL=https://agent.example.com \
  AGENT_HUB_TLS_CERT_FILE=/root/certs/fullchain.pem \
  AGENT_HUB_TLS_KEY_FILE=/root/certs/privkey.pem \
  bash install.sh --mode auto --yes
```

如果提供 HTTPS 域名但不指定证书文件，Caddy 会在域名解析和公网访问条件满足时尝试自动签发证书。安装器不会修改云安全组或主机外部防火墙。

### Docker Compose

```bash
cp deploy/compose/.env.example deploy/compose/.env
# 编辑 .env，至少替换主密钥、JWT 密钥、数据库密码、LiteLLM 密钥和初始化码
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env up -d --build
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env ps
```

Compose 包含迁移、初始化、API、worker、LiteLLM、PostgreSQL、Redis 和 Caddy。容器部署基础设施不等于 Docker Skill 执行后端已经可用；后者还要求本机 daemon 可访问，并存在经过验证的 `agent-hub-skill-runner:latest` 镜像。

离线部署可在联网机器上构建并 `docker save` 以下镜像，再连同 Compose 文件和私密 `.env` 一起传入目标机：

- `agent-hub:latest`
- `agent-hub-skill-runner:latest`（使用经过审核、能够运行 `agent_hub.skills.runner` 的 runner 镜像）
- `postgres:16-alpine`
- `redis:7-alpine`
- `caddy:2-alpine`
- `ghcr.io/berriai/litellm:main-stable`

`.env` 包含密钥，必须按敏感文件保存，不要提交到 Git 或发送到不可信位置。更完整的安装参数见 [安装说明](docs/installation.md)。

### 运维

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub restore /tmp/agent-hub-backup.tar.gz --target /tmp/agent-hub-restore
scripts/agent-hub verify-release
scripts/agent-hub prune-releases --keep 2
scripts/agent-hub prune-releases --keep 2 --execute
```

备份助手只归档 `AGENT_HUB_STATE_DIR`（通常为 `/var/lib/agent-hub`），不包含 PostgreSQL、Redis 或 `/etc/agent-hub/secrets.env`，生产环境必须单独备份数据库和密钥。`backup verify` 只检查压缩包能否读取，`restore` 只解压到显式目标目录供人工检查。

当前 `scripts/agent-hub upgrade` 只是底层版本标记演练，不会下载发布、切换应用或重启服务；应用升级应使用安装器或版本化发布流程。发布清理默认为预演，只有加 `--execute` 才删除旧发布目录，并始终保护当前 `current` 目标。生产环境应同时监控磁盘、数据库备份和外部模型/通道配额。

## 本地开发与测试

要求：Python 3.12、Node.js 22、npm，以及用于完整集成测试的 Docker/Compose。

```bash
uv sync --frozen
npm --prefix web ci

uv run ruff check .
uv run mypy --strict src tests

docker compose -f tests/compose.yml up -d --wait
uv run pytest -q
docker compose -f tests/compose.yml down -v --remove-orphans

npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
```

仓库的 GitHub Actions 质量门会执行 Python lint、严格类型检查、PostgreSQL/Redis 集成测试、前端测试与构建、ShellCheck、Bats 安装器测试和 Compose 配置校验。界面改动还应在桌面与移动端视口执行实际浏览器回归，而不能只依赖组件测试。

## 安全边界

- **密钥**：生产密钥保存在 `/etc/agent-hub/secrets.env`，原生安装权限为 `0600`。不要在普通对话、Skill 包、日志或仓库中粘贴密钥。
- **多租户**：会话、项目、记忆、附件和管理资源按租户隔离；用户级记忆还按 actor 隔离。管理员仍应遵循最小权限原则。
- **文件访问**：运行时只读取已持久化授权的项目工作区或本次运行附件；拒绝路径穿越、特殊文件、硬链接和跨作用域链接，返回内容有大小上限。
- **网络访问**：远程 HTTP/MCP 会拒绝不安全目标和重定向型 SSRF。允许域名不代表目标内容可信。
- **能力安装**：上传包必须通过路径、大小、文件数量、依赖锁、禁止扩展名、可执行声明、权限差异和内容哈希检查；批准安装不等于批准以后每一次高风险调用。
- **执行隔离**：沙箱降低风险但不是绝对安全边界。宿主机、Docker daemon、工作区挂载和远程适配器仍需独立加固。
- **记忆**：Hermes 不应保存秘密，也不能通过记忆提升权限。错误记忆可能影响建议质量，因此保留来源、审批、删除和遗忘入口。
- **外部系统**：模型、飞书、MCP、插件、OpenClaw 和多媒体提供商的数据处理受其服务条款和部署配置约束。
- **安全测试**：Strix 等测试能力不随仓库自动获得。只有在真实 CLI/服务、授权目标、隔离后端和凭证都就绪时才可运行；系统会拒绝伪造可用状态。仅对明确授权的资产进行测试。
- **AI 输出**：模型输出、生成代码和自动修复都可能有误。高影响操作必须保留人工复核、备份和回滚路径。

详细策略见 [安全说明](docs/security.md) 和 [运行时文件读取策略](docs/workspace-file-read-policy.md)。

## 已知限制

- 能力安装器只处理可信目录和受控安装配方，不提供任意互联网软件的无人值守安装。
- Docker 执行依赖真实 daemon 与 runner 镜像；未满足条件时会明确显示不可用。
- 飞书是当前完整验证的外部聊天通道；其他通道需要对应平台配置和适配器验证。
- 多媒体配置项多于已内置的执行客户端；目前实际内置的是 MiniMax/Hailuo 文生视频。
- 大型历史回复会在界面中折叠和限高显示，但系统不会重写已有内容。
- 生产效果受所选模型、上下文窗口、工具权限、网络、预算和第三方服务稳定性影响。

## 文档

- [文档索引](docs/README.md)
- [安装说明](docs/installation.md)
- [运维说明](docs/operations.md)
- [模型池](docs/model-pools.md)
- [Skill 与 MCP](docs/skills-and-mcp.md)
- [Hermes 学习](docs/hermes.md)
- [飞书配置](docs/feishu-setup.md)
- [安全说明](docs/security.md)
- [运行时文件读取策略](docs/workspace-file-read-policy.md)
- [项目能力验收](docs/project-capability-acceptance.md)
- [故障排查](docs/troubleshooting.md)
