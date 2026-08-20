export type ModuleSubItem = {
  to: string;
  label: string;
  permission: string;
};

export type ModuleItem = {
  to: string;
  label: string;
  description: string;
  permission: string;
  children?: ModuleSubItem[];
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
    label: "对话与进化",
    eyebrow: "Conversation & Evolution",
    description: "发起对话、接续会话、查看运行过程，并管理 Skill 蒸馏、长期迭代和进化任务。",
    tone: "cyan",
    modules: [
      {
        to: "/",
        label: "对话",
        description: "连续对话、历史会话、运行过程和附件入口集中在这里。",
        permission: "run:read",
      },
      {
        to: "/evolution",
        label: "进化",
        description: "管理 Skill、Agent、工作流和调度策略的评估、迭代、验证与人工确认。",
        permission: "skill:read",
        children: [
          { to: "/evolution?type=skill", label: "Skill 进化", permission: "skill:read" },
          { to: "/evolution?type=agent", label: "Agent/角色进化", permission: "agent:read" },
          { to: "/evolution?type=workflow", label: "工作流进化", permission: "agent:read" },
          { to: "/evolution?type=scheduler-policy", label: "调度策略进化", permission: "config:read" },
          { to: "/evolution?type=context-memory", label: "多轮记忆策略", permission: "memory:read" },
        ],
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
        children: [
          { to: "/main-agent?section=model", label: "专属模型/API", permission: "config:read" },
          { to: "/main-agent?section=scheduler", label: "调度策略", permission: "config:read" },
          { to: "/main-agent?section=concurrency", label: "并发槽", permission: "config:read" },
          { to: "/main-agent?section=hermes", label: "Hermes 介入", permission: "hermes:read" },
        ],
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
        children: [
          { to: "/workflows?section=list", label: "工作流列表", permission: "agent:read" },
          { to: "/workflows?section=roles", label: "角色配置", permission: "agent:read" },
          { to: "/workflows?section=execution", label: "执行策略", permission: "agent:read" },
          { to: "/workflows?section=review", label: "审查/裁决规则", permission: "agent:read" },
        ],
      },
      {
        to: "/schedules",
        label: "计划任务",
        description: "按指定时间提交任务，可用于报表填写、提醒和需要 OpenClaw 审批的本机操作。",
        permission: "run:create",
      },
      {
        to: "/hermes",
        label: "Hermes 学习",
        description: "按类型查看学习沉淀，确认后再应用到系统行为。",
        permission: "hermes:read",
        children: [
          { to: "/hermes?category=conversation", label: "对话记忆", permission: "hermes:read" },
          { to: "/hermes?category=scheduler", label: "调度观察", permission: "hermes:read" },
          { to: "/hermes?status=pending", label: "待确认学习", permission: "hermes:read" },
          { to: "/hermes?status=confirmed", label: "已确认学习", permission: "hermes:read" },
        ],
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
        description: "配置普通模型、多媒体模型、能力标签、并发容量和供应商预设。",
        permission: "config:read",
        children: [
          { to: "/models?category=text", label: "普通模型", permission: "config:read" },
          { to: "/models?category=multimedia", label: "多媒体模型", permission: "config:read" },
          { to: "/models?section=capabilities", label: "模型能力", permission: "config:read" },
          { to: "/models?section=capacity", label: "并发与容量", permission: "config:read" },
          { to: "/models?section=presets", label: "预设供应商", permission: "config:read" },
        ],
      },

      {
        to: "/memory",
        label: "记忆",
        description: "管理可被 Agent 参考的长期记忆、会话摘要和上下文资源。",
        permission: "memory:read",
      },
      {
        to: "/attachments",
        label: "附件",
        description: "查看和删除从对话页上传的图片、文档、压缩包和上下文文件。",
        permission: "run:read",
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
        description: "上传、安装、审核、搜索、批量管理和启用 Agent 可调用的技能包。",
        permission: "skill:read",
        children: [
          { to: "/skills?view=installed", label: "已安装 Skill", permission: "skill:read" },
          { to: "/skills?view=upload", label: "上传/安装", permission: "skill:read" },
          { to: "/skills?view=permissions", label: "待审批权限", permission: "skill:read" },
          { to: "/skills?view=bulk", label: "批量管理", permission: "skill:read" },
        ],
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
        description: "配置聊天软件接入参数、回调地址、附件获取、回复格式和连接状态。",
        permission: "config:read",
        children: [
          { to: "/channels?provider=feishu&mode=websocket", label: "飞书长连接", permission: "config:read" },
          { to: "/channels?provider=feishu&mode=webhook", label: "Webhook 备用", permission: "config:read" },
          { to: "/channels?section=reply", label: "回复格式", permission: "config:read" },
          { to: "/channels?section=resources", label: "资源识别", permission: "config:read" },
          { to: "/channels?section=test", label: "测试与日志", permission: "audit:read" },
        ],
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
        description: "配置默认模式、日志等级、工具审批、运行期调度和临时 Agent 策略。",
        permission: "config:read",
      },
      {
        to: "/openclaw",
        label: "OpenClaw 控制",
        description: "配置跨平台电脑/服务器接管、权限模式、远程适配器和审批执行控制台。",
        permission: "config:read",
        children: [
          { to: "/openclaw?section=targets", label: "目标设备", permission: "config:read" },
          { to: "/openclaw?section=policy", label: "权限策略", permission: "config:read" },
          { to: "/openclaw?section=sessions", label: "操作会话", permission: "config:read" },
          { to: "/openclaw?section=schedules", label: "计划任务联动", permission: "run:create" },
        ],
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
        description: "查看登录、对话审计、调度、模型、通道、Agent 和系统日志。",
        permission: "audit:read",
        children: [
          { to: "/logs?category=auth", label: "登录日志", permission: "audit:read" },
          { to: "/logs?category=conversation", label: "对话审计", permission: "audit:read" },
          { to: "/logs?category=scheduler", label: "调度日志", permission: "audit:read" },
          { to: "/logs?category=model", label: "模型调用日志", permission: "audit:read" },
          { to: "/logs?category=channel", label: "通道日志", permission: "audit:read" },
        ],
      },
    ],
  },
];
