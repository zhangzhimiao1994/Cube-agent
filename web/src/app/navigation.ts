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
    label: "对话",
    eyebrow: "Workspace",
    description: "发起新对话、接续历史会话、查看运行过程和产物，是日常使用 Agent 的主入口。",
    tone: "cyan",
    modules: [
      {
        to: "/",
        label: "对话与任务",
        description: "自动、直连、派单、讨论、混合五种运行方式统一从这里进入。",
        permission: "run:read",
      },
    ],
  },
  {
    id: "orchestration",
    to: "/orchestration",
    label: "编排",
    eyebrow: "Agent Control",
    description: "主 Agent、角色、工作流和 Hermes 学习都属于 Agent 编排控制层。",
    tone: "green",
    modules: [
      {
        to: "/main-agent",
        label: "主 Agent",
        description: "单独配置主 Agent 模型、控场风格、决策边界和 Hermes 介入策略。",
        permission: "config:read",
      },
      {
        to: "/agents",
        label: "Agent 角色",
        description: "管理导演、文案、剪辑师、经济分析师等可扩展角色。",
        permission: "agent:read",
      },
      {
        to: "/workflows",
        label: "工作流配置",
        description: "配置任务类型、默认角色、执行步骤、交付物和分歧裁决规则。",
        permission: "agent:read",
      },
      {
        to: "/hermes",
        label: "Hermes 学习",
        description: "按时间和会话 ID 查看学习沉淀，确认后再应用到系统行为。",
        permission: "hermes:read",
      },
    ],
  },
  {
    id: "resources",
    to: "/resources",
    label: "资源",
    eyebrow: "Models & Memory",
    description: "模型 API、Key、中转站协议、附件和记忆资源统一在资源层管理。",
    tone: "amber",
    modules: [
      {
        to: "/models",
        label: "模型与 API",
        description: "配置官方模型、自定义中转站、Claude Code/API 协议和可用性测试。",
        permission: "config:read",
      },
      {
        to: "/memory",
        label: "记忆",
        description: "管理可被 Agent 参考的长期记忆、会话摘要和上下文资源。",
        permission: "memory:read",
      },
    ],
  },
  {
    id: "extensions",
    to: "/extensions",
    label: "工具",
    eyebrow: "Tools",
    description: "Skill、MCP 和后续插件入口集中在工具层，便于做权限边界和扩展。",
    tone: "violet",
    modules: [
      {
        to: "/skills",
        label: "Skill",
        description: "上传、安装、审核和启用 Agent 可调用的技能包。",
        permission: "skill:read",
      },
      {
        to: "/mcp",
        label: "MCP",
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
    description: "飞书、Webhook 和后续企业 IM 接入统一放在通道层。",
    tone: "blue",
    modules: [
      {
        to: "/channels",
        label: "通道连接",
        description: "配置聊天软件接入参数、回调地址、附件获取和连接状态。",
        permission: "config:read",
      },
    ],
  },
  {
    id: "system",
    to: "/system",
    label: "系统",
    eyebrow: "System",
    description: "全局设置、用户权限和日志排查收进系统运维入口。",
    tone: "slate",
    modules: [
      {
        to: "/config",
        label: "系统设置",
        description: "配置默认模式、日志等级、工具审批、临场调整和临时 Agent 策略。",
        permission: "config:read",
      },
      {
        to: "/users",
        label: "用户管理",
        description: "管理控制台用户、权限、登录状态和初始管理员保护。",
        permission: "user:read",
      },
      {
        to: "/logs",
        label: "日志中心",
        description: "查看模型、运行、通道、Agent、系统和审计日志。",
        permission: "audit:read",
      },
    ],
  },
];
