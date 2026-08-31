# 魔方 agent 使用说明

魔方 agent 是一个可自部署的多 Agent 工作台。代码包内部仍叫 `agent_hub`，但面向用户的产品名是“魔方 agent”。

它把 Web 控制台、飞书/通道入口、模型池、主 Agent 决策、工作流、Skill/MCP 治理、计划任务、多媒体生成、OpenClaw 电脑/服务器操作、Hermes 学习和审计日志放在同一个系统里。

[English README](README.md)

## 能做什么

- 在 Web 或已接入通道里和主 Agent 对话，继续历史会话，从历史上下文新建分支并上传附件。项目级 Vibe Coding 能力留给后续 harness/runtime 改造；当前 UI 不暴露 Vibe Coding 按钮，后端 metadata 字段保留作未来接入。在没有 harness 的当前版本里，系统可以分析代码、给出修改方案或补丁建议，但不能直接接管仓库完成代码修改、测试、审查和调试闭环。
- 查看派单和讨论运行的子 Agent 工作席。每个席位显示名称、职责、模型、状态、分类摘要和紧凑卡片；点击卡片后再打开详情抽屉。已打开的抽屉会跟随运行数据实时刷新，支持点击遮罩关闭，并在打开时锁住底层页面滚动。
- 把普通模型和多媒体 AI 分开配置，避免把视频生成任务提交给不支持视频的模型。
- 通过能力标签控制图片、视频、音频生成路由。
- 配置 MiniMax/Hailuo 后，使用当前多媒体执行器提交文生视频任务并保存生成文件。
- 通过运行时工具生成 Word 和 PowerPoint 文件：`document.generate_docx`、`presentation.generate_pptx`。
- 上传单个 Skill 或多个 Skill 打包的 `.zip`、`.tar`、`.tar.gz`、`.tgz`，扫描后再审批启用。同名 Skill 默认只保留最新可用版本；上传冲突时提示选择覆盖，或保留为可控的不同版本。
- 通过 OpenClaw 功能开关、权限模式、命令白名单和适配器，受控执行服务器或电脑操作。
- 创建一次性计划任务或 cron 周期任务；对话里识别到带明确日期/时间/周期和可执行动作的计划需求时，先给出计划方案，再由用户确认创建。
- 在日志中心查看模型、模式、功能、Agent、通道和审计日志。用户触发对话会记录 `run.submit` 审计事件，能查到哪个用户提交了哪个对话。

## 快速安装

在干净的 Linux 服务器上执行：

```bash
git clone https://github.com/zhangzhimiao1994/CubeAgent.git
cd CubeAgent
sudo bash install.sh --mode auto --yes
```

`auto` 会优先在支持的 systemd Linux 上使用原生部署：Ubuntu 22.04/24.04、Debian 12/13、Rocky Linux 9、AlmaLinux 9。Docker 模式保留为可选兜底。

服务器没有 `git` 时，可以用 GitHub 压缩包安装：

```bash
tmp="$(mktemp -d /tmp/agent-hub-install.XXXXXX)"
curl -fL https://github.com/zhangzhimiao1994/CubeAgent/archive/refs/heads/main.tar.gz -o "$tmp/source.tar.gz"
mkdir -p "$tmp/source"
tar -xzf "$tmp/source.tar.gz" --strip-components=1 -C "$tmp/source"
cd "$tmp/source"
sudo bash install.sh --mode auto --yes
```

不要把压缩包直接用 `--strip-components=1` 解压到 `/root`，否则源码结构会被打平，后续命令会找不到正确目录。

国内服务器可以启用镜像模式：

```bash
sudo env AGENT_HUB_MIRROR_MODE=auto bash install.sh --mode auto --yes
```

如果要绑定自己的 HTTPS 证书：

```bash
sudo env AGENT_HUB_PUBLIC_URL=https://agent.example.com \
  AGENT_HUB_TLS_CERT_FILE=/root/certs/fullchain.pem \
  AGENT_HUB_TLS_KEY_FILE=/root/certs/privkey.pem \
  bash install.sh --mode auto --yes
```

安装完成后，脚本会输出管理地址 `/setup` 和一次性初始化码。进入 `/setup` 创建第一个超级管理员。

## Docker 离线初始部署包

如果目标机器不能访问公网，但可以运行 Docker，可以先在一台有网络的机器上构建并打包镜像。这个离线包用于启动一个全新的空系统；它不会包含当前服务器的历史数据库、用户、模型配置、Hermes+ 记忆、附件或密钥。

### 1. 在有网络的构建机器准备配置

```bash
cd CubeAgent
cp deploy/compose/.env.example deploy/compose/.env
```

编辑 `deploy/compose/.env`，至少替换这些占位值：

- `AGENT_HUB_MASTER_KEY`
- `AGENT_HUB_JWT_SIGNING_KEY`
- `POSTGRES_PASSWORD`
- `LITELLM_MASTER_KEY`
- `AGENT_HUB_SETUP_CODE`

可以用下面的命令生成随机值：

```bash
openssl rand -base64 32
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
```

`deploy/compose/litellm.yaml` 默认是空模型列表：

```yaml
model_list: []
litellm_settings:
  drop_params: true
  request_timeout: 600
```

离线环境如果要调用模型，需要在 Web 控制台配置本地模型、内网中转站或可访问的供应商 API。断网环境不会自动拥有外部模型能力。

DOCX/PPTX 生成本身使用确定性的 Python 标准库 OOXML 构建器，离线 Docker 镜像不需要为了 Office 文件生成再下载额外公网包。但全新离线安装仍然需要配置本地、内网或其他可访问模型，主 Agent 才能规划并调用这些工具。生成文件会写入 `generated_artifact_dir`，默认是 `/var/lib/agent-hub/generated-artifacts`，在 Docker 部署中由 `agent_hub_data` volume 持久化。

### 2. 构建和拉取所有镜像

```bash
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env build
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env pull postgres redis caddy litellm
```

需要离线带走的镜像包括：

- `agent-hub:latest`
- `postgres:16-alpine`
- `redis:7-alpine`
- `caddy:2-alpine`
- `ghcr.io/berriai/litellm:main-stable`

### 3. 导出离线镜像包和部署配置

```bash
docker save \
  agent-hub:latest \
  postgres:16-alpine \
  redis:7-alpine \
  caddy:2-alpine \
  ghcr.io/berriai/litellm:main-stable \
  -o mofang-agent-offline-images.tar

tar -czf mofang-agent-offline-compose.tgz \
  deploy/compose/docker-compose.yml \
  deploy/compose/Caddyfile \
  deploy/compose/healthcheck.sh \
  deploy/compose/litellm.yaml \
  deploy/compose/.env
```

`mofang-agent-offline-compose.tgz` 里包含 `.env`，应按密钥文件处理，不要提交到 Git，也不要发到不可信位置。

### 4. 在离线目标机器导入并启动

```bash
mkdir -p /opt/mofang-agent
cd /opt/mofang-agent

docker load -i /path/to/mofang-agent-offline-images.tar
tar -xzf /path/to/mofang-agent-offline-compose.tgz --strip-components=2

chmod 600 .env
docker compose --env-file .env up -d
docker compose --env-file .env ps
```

第一次启动时，`migrate` 服务会执行 `alembic upgrade head` 创建空数据库结构，`bootstrap` 服务会用 `.env` 里的 `AGENT_HUB_SETUP_CODE` 准备初始化入口。进入 `AGENT_HUB_PUBLIC_URL/setup` 创建第一个超级管理员。

如果你只想离线启动基础系统，不需要迁移线上状态，就不需要导出生产数据库或 Docker volumes。

## 首次配置顺序

1. 登录 Web 控制台。
2. 进入“模型配置”，先添加至少一个普通模型，供主 Agent 使用。
3. 进入“主 Agent”，选择主模型、控制模式、决策策略、Hermes 策略和复核轮次。
4. 进入“系统设置”，按需要打开多媒体生成、OpenClaw。Vibe Coding 配置暂不在当前 UI 暴露，后端字段保留作后续 harness/runtime 接入。
5. 按业务需要继续配置 Skill、MCP、通道、多媒体模型、计划任务、记忆和用户。

## 模型配置

模型分两大类。

**普通模型**用于普通对话、推理、工具调用、结构化输出、代码任务，以及在模型能力打开后进行图片/语音理解。常见供应商包括 OpenAI、DeepSeek、Anthropic、Kimi/Moonshot、阿里 Qwen/DashScope、阿里 Token Plan、MiniMax、OpenAI 兼容中转站、Anthropic Messages 中转站。

**多媒体 AI**用于生成任务，不作为普通聊天模型使用。预设包括 Sora、OpenAI Audio、MiniMax Hailuo、MiniMax Audio、Google Veo、Kling、阿里 Wan、Seedance、Seedream、中转站和自定义服务商。

关键能力标签：

- `image_generation`：图片生成。
- `video_generation`：视频生成。
- `audio_generation`：语音/音频生成。
- `text`、`tool_calling`、`structured_output`：普通模型能力。

系统会根据能力标签和已知模型名单判断是否允许提交视频生成。没有视频生成能力的模型不会收到视频生成任务。

当前真实接入的多媒体执行客户端是 MiniMax/Hailuo 文生视频。其他多媒体预设可以保存配置并参与能力门控，后续通过通用多媒体 provider 接口继续扩展执行客户端。

Office 文件生成是运行时工具能力，不是新的 `ModelCapability`。模型仍只需要标注 `text`、`tool_calling`、`structured_output` 等普通能力；文档和演示稿任务应路由给可工具调用的写作/设计角色，再调用：

- `document.generate_docx`：根据结构化文档蓝图生成真实 DOCX 文件。
- `presentation.generate_pptx`：根据结构化幻灯片蓝图生成真实 PPTX 文件。

内置 PPT 模板包括 `consulting-clean`、`technical-blueprint`、`dark-launch`。生成文件会带文件 metadata 和受认证保护的 `download_url`；Web 控制台会在运行详情、对话输出和子 Agent 工作席里以文件卡片显示并提供下载。

## 对话、进化和模式

首页就是实际工作台。左侧抽屉用于切换模块，右侧抽屉用于打开历史会话。历史会话名称使用“首次问题 + 时间戳”，避免多个相似主题只显示会话 ID。

对话页支持以下模式：

- `auto`：主 Agent 根据任务决定执行方式。
- `direct`：直接使用选定模型回答。
- `dispatch`：派给配置好的 Agent 角色。
- `discuss`：走讨论式流程。
- `hybrid`：结合派单和讨论。

历史会话行提供按原思路新建分支的入口；分支引用激活后，输入区只显示明确的取消引用控件，不再要求每条消息手动打开 Handoff。完整的项目级 Vibe Coding（生成项目、读写代码、运行测试、审查、调试并再次验证）留给后续 harness/runtime 改造：当前 UI 不暴露 Vibe Coding 按钮，后端 metadata 字段保留作未来接入。在 harness 接入前，代码类请求属于建议型能力：系统可以读取用户提供的代码上下文、说明问题、制定实现计划、生成补丁建议或文件产物，但不会直接修改用户源码树。正在运行的对话可以在输入区停止生成；对话里检测出的计划任务或进化任务提案，也可以在创建持久记录前取消。

派单和讨论运行会在对话流里显示紧凑的运行过程卡片。点击后进入子 Agent 工作席：每个 Agent 独立显示工作中、等待中、失败、已下班等状态。抽屉默认展示分类摘要和重点卡片，详细事件字段、工具 payload、模型元数据和产物元数据不会直接铺开；需要点开对应卡片并展开完整字段后查看。这样既能减少长流程的 UI 噪音，也保留排查和审计所需的信息。

长对话由对话框架处理，不归进化模块处理。当历史内容接近主 Agent 模型上下文窗口时，系统会自动压缩旧轮次，保留初始目标、长期约束和最新结论，再把压缩上下文传入下一轮。

进化用于长期资产改进：Skill 蒸馏、Darwin 式迭代、Agent/工作流/提示词优化、候选版本评分验收。普通问答、一次性方案和普通资料整理不会默认进入进化，除非用户明确要创建或改进一个可复用资产。

如果用户在对话里提出带明确时间、日期或周期，并且包含可执行动作的计划任务，例如“每天 9 点提醒我填报表”“每周一生成周报”或“9 月 3 号生成一个方案”，系统会先生成计划方案，用户确认后才写入计划任务。普通讨论计划任务设计或排查误判的问题，会留在当前对话里继续回答。

Skill 创建也应该从对话开始。例如：“我想生成一个 AI 科研 Skill”。主 Agent 会先收集目标、资料来源、验收任务和权限边界，再创建有依据的进化任务，而不是直接安装未经验证的 Skill。

## OpenClaw

OpenClaw 是系统级功能开关，用于受控操作服务器或电脑，不是普通工作流。

支持的操作类型：

- `server_command`：服务器命令。
- `desktop_action`：桌面操作。
- `screen_read`：读取屏幕。
- `file_read`：读取文件。

权限模式：

- `ask`：默认需要用户审批。
- `read_only`：只读模式。
- `auto_review`：低风险自动审核，高风险仍需审批。
- `trusted_auto`：可信环境自动执行，需谨慎使用。

OpenClaw 使用命令白名单、适配器配置、会话状态和审计记录。Linux 服务器命令可以通过本地适配器执行；远程适配器在配置 `OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON` 后，也可以在无需 argv 命令的情况下执行受限 `file_read`，返回内容受 `OPENCLAW_ADAPTER_FILE_READ_LIMIT_BYTES` 限制。屏幕读取可以通过 `OPENCLAW_ADAPTER_SCREEN_READ_COMMAND_JSON` 配置为适配器侧固定驱动命令，Cube Agent 发起 `screen_read` 时不下发任意 argv。桌面动作同样可以通过 `OPENCLAW_ADAPTER_DESKTOP_ACTION_COMMAND_JSON` 配置为固定驱动命令；适配器会把受控的 operation JSON 通过 stdin 传给该驱动。Windows、Linux 桌面、macOS、屏幕和文件系统接管应通过远程适配器连接，并配置独立凭证和最小权限。每个远程适配器必须在 `/v1/openclaw/health` 返回平台和支持的能力清单；Cube Agent 会在执行前校验该健康响应，避免把不支持的桌面、屏幕或文件操作当成可用能力。

常用命令：

```bash
scripts/agent-hub openclaw-adapter
```

## Skill 和 MCP

Skill 必须先上传、扫描、审批，再进入可用列表。

支持的外层压缩包：

- `.zip`
- `.tar`
- `.tar.gz`
- `.tgz`

一个压缩包可以是单个 Skill，也可以包含多个 Skill 目录。多 Skill 压缩包允许存在多层目录；扫描器会寻找有效的 Skill manifest，并把不能识别的项目列为 skipped。

每个 Skill 仍然会进行安全检查：路径穿越、压缩包大小、解压大小、文件数量、依赖锁定、禁止扩展名、可执行文件声明、权限 diff。审批通过后才会启用。

Skill 名称默认去重：同名 Skill 的最新审批版本作为当前可用版本。上传时如果与已有名称冲突，系统会提示选择覆盖当前版本，或保留为一个可选择的不同版本。只有确实需要运维回退或灰度时才建议保留多版本；普通场景保持最新版本，避免路由时出现重复能力。

MCP 独立配置，包括 transport、命令或 URL、允许工具、可执行文件白名单、域名白名单和超时。

## 通道

通道层负责把外部聊天入口连接到主 Agent。当前控制台包含飞书、钉钉、企业微信、微信、Telegram、Slack、QQ 和自定义 Webhook 的配置入口。飞书有完整的首发接入链路。

飞书推荐使用长连接模式，只需要 App ID 和 App Secret。Webhook 保留为备用方式，用于需要平台 URL 校验或事件加密的场景。

通道消息现在先进入主 Agent。通道层不再根据命令文字直接选择直连、派单、讨论、混合、Vibe Coding、帮助、OpenClaw 或计划模式；主 Agent 会基于完整消息判断入口和路由。同一个通道会话里的后续消息默认沿用最近一次已确定的模式；明确说“切换到讨论模式/混合模式”等会切换模式，明确说“新建对话/换个话题/重新开始”会回到新的主 Agent 入口判断。

如果用户需要指定资源，只在消息最开头连续写资源选择器：

- `@github`：请求插件。
- `&research`：请求 Skill。
- `#filesystem`：请求 MCP 服务。

例如 `@github &research #filesystem 梳理这个仓库的改造计划` 会附带资源提示，同时保留原始消息文本。正文开始后出现的 `@`、`&`、`#` 都按普通文本处理，所以 `C#`、`#标题`、`@某人` 不会被误识别成资源调用。

飞书回复会对结构化运行段落和 Markdown 表格使用 rich post。长普通文本会拆成多条回复；超长 Markdown 表格会在完整表格行边界截断并附带提示，避免把表格行切成半截。

旧的飞书字段 `FEISHU_COMMAND_ALIASES` 只作为历史配置兼容保留。已保存的别名不再作为生效路由命令，也不会显示为当前通道命令。

飞书配置见 [docs/feishu-setup.md](docs/feishu-setup.md)。

## 计划任务

计划任务支持：

- 一次性任务：指定执行时间。
- cron 周期任务：支持日常和每周这类固定节奏。
- 时区配置。
- 误点策略：补跑一次或跳过。
- 预算和工作流绑定。

计划任务只是普通任务的提交者，不能绕过模型容量、路由、工具审批、OpenClaw 审批或 Skill 权限边界。

## 日志、审计和 Hermes

日志中心按模块拆分：审计日志、模型配置与调用错误、模式运行错误、主要功能错误、Agent 角色错误、通道连接错误。列表支持搜索、列筛选、排序、多选和导出 JSON。

审计日志会记录管理操作和用户触发的对话。`run.submit` 记录包含：

- 用户 ID 和用户角色。
- 运行 ID。
- 当前对话 ID 和参考对话 ID。
- 请求模式和最终接受模式。
- 工作流、Agent 列表、直连模型、供未来接入使用的 Vibe Coding 后端 metadata 标记、附件数量。
- 消息预览和消息 SHA-256 哈希。

Hermes 学习独立于对话页面。对话记忆和调度观察会进入不同记录类别；记录支持筛选、排序、单条确认、单条删除、批量确认和批量删除。

## 附件管理

对话附件上传后可以在附件管理页面查看和删除。压缩包既可以作为普通附件上传，也可以在明确点击“作为 Skill 安装”后进入 Skill 安装扫描流程。

普通附件和 Skill 安装是两条不同路径：普通附件用于给对话提供上下文；Skill 安装用于把能力加入系统。

## 运维命令

原生安装后常用命令：

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub restore /tmp/agent-hub-backup.tar.gz
scripts/agent-hub upgrade
```

更多内容见 [docs/operations.md](docs/operations.md) 和 [docs/installation.md](docs/installation.md)。

## 安全边界

- 安装器不会修改云厂商安全组。是否开放公网端口，需要在云控制台里手动确认。
- API Key 和密钥只应该写入模型配置/密钥配置，不要通过普通对话提交。
- OpenClaw、Skill、MCP、工具调用都有权限、白名单、审批和审计记录。
- 日志和审计详情会尽量避免明文密钥泄漏。

更多内容见 [docs/security.md](docs/security.md)。

## 开发命令

```bash
uv run ruff check .
uv run mypy --strict src tests
uv run pytest -q
npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
```

## 相关文档

- [安装说明](docs/installation.md)
- [运维说明](docs/operations.md)
- [模型池](docs/model-pools.md)
- [Skill 和 MCP](docs/skills-and-mcp.md)
- [Hermes](docs/hermes.md)
- [飞书配置](docs/feishu-setup.md)
- [安全说明](docs/security.md)
- [故障排查](docs/troubleshooting.md)
