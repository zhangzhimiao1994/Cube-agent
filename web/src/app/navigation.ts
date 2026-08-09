export type ModuleItem = {
  to: string;
  label: string;
  description: string;
  permission: string;
};

export type ModuleGroup = {
  id: string;
  to: string;
  label: string;
  eyebrow: string;
  description: string;
  tone: "cyan" | "green" | "amber" | "violet" | "blue" | "slate";
  modules: ModuleItem[];
};

export const MODULE_GROUPS: ModuleGroup[] = [
  {
    id: "workspace",
    to: "/workspace",
    label: "工作台",
    eyebrow: "Operations",
    description: "发起对话任务、查看运行状态，把日常操作集中到一个入口。",
    tone: "cyan",
    modules: [
      {
        to: "/",
        label: "对话",
        description: "直连、派单、讨论、混合模式的任务发起与运行详情。",
        permission: "run:read",
      },
    ],
  },
  {
    id: "orchestration",
    to: "/orchestration",
    label: "编排",
    eyebrow: "Agent control",
    description: "主 Agent、角色、工作流和 Hermes 学习都属于编排控制层。",
    tone: "green",
    modules: [
      {
        to: "/main-agent",
        label: "主 Agent",
        description: "独立模型、控场风格、决策规则和 Hermes 介入策略。",
        permission: "config:read",
      },
      {
        to: "/agents",
        label: "Agent 角色",
        description: "导演、文案、剪辑师、分析师等可扩展角色。",
        permission: "agent:read",
      },
      {
        to: "/workflows",
        label: "工作流配置",
        description: "任务类型、角色池、步骤、交付物和临场调整核对。",
        permission: "agent:read",
      },
      {
        to: "/hermes",
        label: "Hermes 学习",
        description: "按会话和时间查看学习沉淀，确认后再应用。",
        permission: "hermes:read",
      },
    ],
  },
  {
    id: "resources",
    to: "/resources",
    label: "资源",
    eyebrow: "Models & memory",
    description: "模型 API、Key、中转站协议和记忆资源放在这里管理。",
    tone: "amber",
    modules: [
      {
        to: "/models",
        label: "模型与 API",
        description: "官方模型、自定义中转站、Claude Code/API 协议和可用性测试。",
        permission: "config:read",
      },
      {
        to: "/memory",
        label: "记忆",
        description: "管理可被 Agent 参考的长期记忆与上下文资源。",
        permission: "memory:read",
      },
    ],
  },
  {
    id: "extensions",
    to: "/extensions",
    label: "工具",
    eyebrow: "Tools",
    description: "Skill 与 MCP 工具集中管理，便于做权限边界和扩展。",
    tone: "violet",
    modules: [
      {
        to: "/skills",
        label: "技能",
        description: "上传、隔离、审核和启用 Agent 可使用的 Skill。",
        permission: "skill:read",
      },
      {
        to: "/mcp",
        label: "MCP 工具",
        description: "管理外部工具连接、权限和可调用能力。",
        permission: "mcp:read",
      },
    ],
  },
  {
    id: "channels",
    to: "/channels-hub",
    label: "通道",
    eyebrow: "Channels",
    description: "飞书、企业 IM、Webhook 等聊天入口统一放在通道层。",
    tone: "blue",
    modules: [
      {
        to: "/channels",
        label: "通道连接",
        description: "配置飞书等聊天软件接入参数、回调地址和连接状态。",
        permission: "config:read",
      },
    ],
  },
  {
    id: "system",
    to: "/system",
    label: "系统",
    eyebrow: "System",
    description: "设置、用户和日志排查收进系统运维入口。",
    tone: "slate",
    modules: [
      {
        to: "/config",
        label: "设置",
        description: "默认模式、日志等级、工具审批和全局运行参数。",
        permission: "config:read",
      },
      {
        to: "/users",
        label: "用户",
        description: "管理控制台用户、权限和登录状态。",
        permission: "user:read",
      },
      {
        to: "/logs",
        label: "日志",
        description: "模型、运行、通道、Agent、系统和审计日志。",
        permission: "audit:read",
      },
    ],
  },
];
