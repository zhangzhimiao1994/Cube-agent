# Capability Installer Design

## Goal

让主 Agent 在发现缺少某类工具能力时，可以从可信能力目录生成安装方案，经人工确认后安装插件、校验运行时可用性，并把新能力暴露给后续任务使用。

## User Experience Contract

- 能力发现和安装确认的主流程应在会话交互区完成；插件/MCP 页面只作为管理后台和故障排查入口。
- 用户可以在管理界面搜索“我需要某能力”，系统返回中文候选、用途、风险、所需权限、沙箱和来源。
- 主 Agent 在会话中判断缺少能力时，应直接给出可安装能力卡片，用户无需先离开会话去插件模块查找。
- 用户可以从候选生成安装计划。安装计划必须明确会安装什么插件、开放哪些 capability、是否需要二次动作审批、失败时如何回滚。
- 插件安装必须可取消；未确认的计划不能静默安装。
- 安装后，能力应进入现有 runtime capability manifest，主 Agent 后续可通过现有能力边界调用。
- 失败安装不得留下启用中的半成品插件；失败原因要可读，并保留审计记录。
- 高风险能力默认 `require_approval`，安装确认不等于以后每次执行都自动同意。

## Backend Contract

- 新增可信目录层，目录项使用稳定 id、中文名称、能力别名、风险等级、来源、插件模板和校验信息。
- 新增安装编排层，负责：
  - resolve：把能力描述或 capability id 映射为候选目录项；
  - plan：生成安装计划，不产生运行时副作用；
  - install：在确认后安装或更新插件，并执行健康检查；
  - rollback：安装失败或用户手动回滚时恢复安装前插件状态。
- 安装流程复用现有 `PluginResourceRequest`、`PluginCapabilityRequest`、插件包审批状态、`RuntimePluginService` 和 capability manifest source。
- 第一版只支持可信目录里的 manifest-only/http_json/已登记 adapter package 模板，不支持任意 URL 下载和执行任意代码。
- 所有安装、失败、回滚、健康检查结果写入 admin audit。

## Frontend Contract

- 插件/MCP 管理页增加“能力安装器”区域：
  - 搜索缺失能力；
  - 展示候选卡片；
  - 展示安装计划；
  - 确认安装、取消、回滚、重新检测。
- 运行详情中的缺能力/插件不可用提示可以跳转到对应安装计划。
- 所有危险权限和审批策略用中文短标签展示，避免把 JSON 原文直接堆到用户面前。

## Safety Rules

- 目录项默认来自本地可信目录；远端目录或第三方市场后续必须有 allowlist、hash/signature 校验和审批。
- 安装计划不可直接携带 secret 明文，只能引用 `credential_ref`。
- 安装成功后也要保留 capability policy；`require_approval` 能力仍走动作审批。
- `security_testing`、`code_execution`、`account_write`、`production_change` 等风险必须默认 require approval。
- 回滚只能恢复本系统记录的安装前状态，不删除用户自行创建的同名插件，除非该插件明确由本次安装创建。

## Non-goals

- 不实现任意互联网上的插件自动下载。
- 不绕过现有权限、审批、沙箱和签名检查。
- 不把 Strix 或任何具体渗透工具内置成默认启用能力。
- 不在这个切片重构整个插件管理页面。

## Acceptance

- API 能列出可信能力目录、搜索能力、生成安装计划、取消计划、确认安装、查询状态、回滚。
- 安装 manifest-only/http_json 插件后，能力进入 `/api/v1/admin/capabilities/manifest`。
- 缺少能力时，运行详情能携带可跳转的安装建议。
- 前端能完成搜索、计划、取消、安装、回滚的主流程。
- 后端、前端 focused tests 通过，并完成一次移动端和桌面端 UI 实测。
