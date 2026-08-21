import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError, type ChannelStatus } from "../api/client";
import { useNavSection } from "../app/navSections";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

type ChannelGuide = {
  purpose: string;
  auth: string;
  docUrl: string;
  consoleUrl?: string;
  consolePath: string[];
  fields: { env: string; label: string; secret?: boolean; placeholder: string; source: string; options?: { value: string; label: string }[] }[];
  steps: string[];
  verify: string[];
};

const STATUS_LABELS: Record<string, string> = {
  configured: "已接通",
  missing_config: "待配置",
};

const CHANNEL_GUIDES: Record<string, ChannelGuide> = {
  feishu: {
    purpose: "支持飞书长连接和 Webhook。长连接与 CowAgent/OpenClaw 一样只需要 App ID / App Secret；Webhook 才需要事件订阅校验信息和公网地址。",
    auth: "长连接：App ID + App Secret；Webhook：飞书签名 + Verification Token + 公网 URL，Encrypt Key 按是否开启事件加密填写",
    docUrl: "https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?lang=zh-CN",
    consoleUrl: "https://open.feishu.cn/app",
    consolePath: [
      "开发者后台 → 我的应用 → 选择机器人入口对应的应用 → 凭证与基础信息",
      "推荐：事件与回调 → 选择长连接模式，添加接收消息事件",
      "自建应用：事件与回调 → 加密策略 → 复制 Verification Token；如开启事件加密，再复制 Encrypt Key",
      "Webhook 备用：事件与回调 → 事件订阅 → 将事件发送至开发者服务器",
    ],
    fields: [
      {
        env: "FEISHU_APP_TYPE",
        label: "应用类型",
        placeholder: "custom_app",
        source: "自建应用选择 custom_app；机器人模板应用选择 bot_template",
        options: [
          { value: "custom_app", label: "自建应用" },
          { value: "bot_template", label: "机器人模板应用" },
        ],
      },
      {
        env: "FEISHU_TRANSPORT",
        label: "接收方式",
        placeholder: "websocket",
        source: "长连接 websocket 只需要 App ID / App Secret；Webhook 需要公网回调校验",
        options: [
          { value: "websocket", label: "长连接" },
          { value: "webhook", label: "Webhook" },
          { value: "both", label: "长连接 + Webhook" },
        ],
      },
      { env: "FEISHU_APP_ID", label: "App ID", placeholder: "cli_xxx", source: "凭证与基础信息 → App ID；机器人模板应用也会提供" },
      { env: "FEISHU_APP_SECRET", label: "App Secret", secret: true, placeholder: "飞书应用密钥", source: "凭证与基础信息 → App Secret；机器人模板应用也会提供" },
      { env: "FEISHU_VERIFICATION_TOKEN", label: "Verification Token", secret: true, placeholder: "自建应用事件订阅校验 token", source: "自建应用 → 事件与回调 → 加密策略 → Verification Token" },
      { env: "FEISHU_ENCRYPT_KEY", label: "Encrypt Key", secret: true, placeholder: "启用事件加密时填写", source: "自建应用 → 事件与回调 → 加密策略 → Encrypt Key；未启用加密可留空" },
      { env: "AGENT_HUB_PUBLIC_URL", label: "公网访问地址", placeholder: "https://agent.example.com", source: "Webhook 回调需要公网地址；长连接模式可留空" },
    ],
    steps: [
      "推荐长连接：在飞书事件与回调中选择长连接模式，系统只需要 App ID 和 App Secret。",
      "Webhook 备用：配置事件订阅 Request URL，并补充 Verification Token；如果开启事件加密，再填写 Encrypt Key。",
      "在本页保存后系统会刷新飞书长连接；如果改的是服务器环境文件，再重启 API 服务。",
      "接入验证通过后，在飞书里给机器人发送文本消息，任务页应出现新运行记录。",
    ],
    verify: ["长连接模式：飞书后台长连接保存成功并能接收消息", "Webhook 模式：飞书后台 URL 验证通过", "发送文本后任务页出现新任务", "失败时查看系统日志中的 channel=feishu"],
  },
  dingtalk: {
    purpose: "用于钉钉机器人或应用事件入口。",
    auth: "共享 Webhook Token，服务端校验 x-agent-hub-channel-token 或 token 参数",
    docUrl: "https://open.dingtalk.com/document/development/event-subscription-enables-disables-application-events",
    consoleUrl: "https://open-dev.dingtalk.com/",
    consolePath: [
      "应用开发 → 企业内部应用/机器人 → 选择应用",
      "应用信息 → 复制 AppKey / AppSecret",
      "事件订阅或开发管理 → HTTP 回调 → 填写本页 Webhook 地址",
    ],
    fields: [
      { env: "DINGTALK_APP_KEY", label: "App Key", placeholder: "ding app key", source: "应用信息 → AppKey / Client ID" },
      { env: "DINGTALK_APP_SECRET", label: "App Secret", secret: true, placeholder: "ding app secret", source: "应用信息 → AppSecret / Client Secret" },
      { env: "DINGTALK_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token", source: "魔方 agent 自生成共享 token，并同步填入回调 URL token 参数或请求头" },
    ],
    steps: [
      "在钉钉开放平台或机器人配置中创建事件回调。",
      "Webhook 指向本页地址，并带上配置的 token。",
      "发送文本事件后，到任务页查看是否创建运行记录。",
    ],
    verify: ["钉钉后台保存回调成功", "Webhook 测试返回 2xx", "任务页出现钉钉来源的新任务"],
  },
  wecom_bot: {
    purpose: "用于企业微信群智能机器人。",
    auth: "共享 Webhook Token",
    docUrl: "https://developer.work.weixin.qq.com/document/path/91770",
    consoleUrl: "https://work.weixin.qq.com/wework_admin/frame",
    consolePath: [
      "企业微信群 → 群设置 → 群机器人 → 添加机器人",
      "复制机器人 Webhook 地址中的 key 参数",
      "魔方 agent 中生成共享 Webhook Token 后写入服务器环境",
    ],
    fields: [
      { env: "WECOM_BOT_WEBHOOK_KEY", label: "Webhook Key", secret: true, placeholder: "企微机器人 key", source: "群机器人 Webhook URL 中的 key" },
      { env: "WECOM_BOT_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token", source: "魔方 agent 自生成共享 token，用于保护入站 Webhook" },
    ],
    steps: [
      "在企业微信群添加机器人并取得 Webhook Key。",
      "把回调地址设置为本页 Webhook，并配置共享 token。",
      "在群内发送测试文本，检查任务页和运行详情。",
    ],
    verify: ["群内发送测试消息", "任务页出现企微机器人来源的新任务", "失败时检查 key 与 token 是否同时配置"],
  },
  wecom_app: {
    purpose: "把企业微信内部私聊、审批入口和任务流连接到主 Agent；底层使用企业微信应用凭证完成回调校验。",
    auth: "企业微信 SHA1 回调签名",
    docUrl: "https://developer.work.weixin.qq.com/document/path/90238",
    consoleUrl: "https://work.weixin.qq.com/wework_admin/frame",
    consolePath: [
      "企业微信管理后台 → 应用管理 → 选择要连接 Agent 的内部应用",
      "应用详情 → 复制 AgentId 和 Secret",
      "我的企业 → 企业信息 → 复制企业 ID",
      "应用详情 → 接收消息 → 设置 API 接收 → 填写 URL / Token",
    ],
    fields: [
      { env: "WECOM_CORP_ID", label: "Corp ID", placeholder: "wwxxxx", source: "我的企业 → 企业信息 → 企业 ID" },
      { env: "WECOM_AGENT_ID", label: "Agent ID", placeholder: "1000002", source: "应用管理 → 应用详情 → AgentId" },
      { env: "WECOM_SECRET", label: "Secret", secret: true, placeholder: "企业微信应用 secret", source: "应用管理 → 应用详情 → Secret" },
      { env: "WECOM_TOKEN", label: "Token", secret: true, placeholder: "回调 token", source: "接收消息 → 设置 API 接收 → Token" },
    ],
    steps: [
      "在企业微信管理后台准备一个内部应用作为 Agent 入口。",
      "把接收消息服务器 URL 设置为本页 Webhook。",
      "在本页保存 token 和 secret 后发送测试消息；如果改的是服务器环境文件，再重启服务。",
    ],
    verify: ["企业微信 URL 校验通过", "应用私聊发送文本后任务页出现新任务", "失败时检查 CorpID/AgentID/Token 是否来自同一个应用"],
  },
  wechat_official: {
    purpose: "用于微信公众号消息入口。",
    auth: "微信 SHA1 回调签名",
    docUrl: "https://developers.weixin.qq.com/doc/offiaccount/Basic_Information/Access_Overview.html",
    consoleUrl: "https://mp.weixin.qq.com/",
    consolePath: [
      "公众号后台 → 设置与开发 → 基本配置",
      "公众号开发信息 → 复制 AppID / AppSecret",
      "服务器配置 → 修改配置 → 填写 URL / Token",
    ],
    fields: [
      { env: "WECHATMP_APP_ID", label: "App ID", placeholder: "公众号 app id", source: "基本配置 → 公众号开发信息 → AppID" },
      { env: "WECHATMP_APP_SECRET", label: "App Secret", secret: true, placeholder: "公众号 app secret", source: "基本配置 → 公众号开发信息 → AppSecret" },
      { env: "WECHATMP_TOKEN", label: "Token", secret: true, placeholder: "服务器配置 token", source: "服务器配置 → Token，自定义后两边保持一致" },
    ],
    steps: [
      "在公众号后台启用服务器配置。",
      "URL 填本页 Webhook，Token 填环境变量中的 WECHATMP_TOKEN。",
      "关注公众号后发送文本消息，到任务页核验。",
    ],
    verify: ["公众号后台服务器配置启用成功", "关注后发送文本消息", "任务页出现公众号来源的新任务"],
  },
  wechat_customer_service: {
    purpose: "用于微信客服入口。",
    auth: "微信客服 SHA1 回调签名",
    docUrl: "https://developer.work.weixin.qq.com/document/path/94638",
    consoleUrl: "https://work.weixin.qq.com/kf/",
    consolePath: [
      "微信客服后台 → 开发配置 / API 配置",
      "企业微信管理后台 → 我的企业 → 企业信息 → 企业 ID",
      "复制 Secret / Token，并把回调 URL 填为本页 Webhook",
    ],
    fields: [
      { env: "WECHAT_KF_CORP_ID", label: "Corp ID", placeholder: "企业 ID", source: "企业微信管理后台 → 我的企业 → 企业 ID" },
      { env: "WECHAT_KF_SECRET", label: "Secret", secret: true, placeholder: "客服 secret", source: "微信客服后台 → 开发配置 → Secret" },
      { env: "WECHAT_KF_TOKEN", label: "Token", secret: true, placeholder: "回调 token", source: "微信客服后台 → 开发配置 → Token" },
    ],
    steps: [
      "在微信客服后台配置回调地址。",
      "按本页字段配置企业 ID、Secret 和 Token。",
      "通过客服入口发送测试文本，检查任务是否进入队列。",
    ],
    verify: ["微信客服回调配置保存成功", "客服入口发送消息后任务页出现新任务", "失败时检查 CorpID 与客服账号归属"],
  },
  telegram: {
    purpose: "用于 Telegram Bot Webhook。",
    auth: "Telegram Secret Token 请求头",
    docUrl: "https://core.telegram.org/bots/api#setwebhook",
    consoleUrl: "https://t.me/BotFather",
    consolePath: [
      "Telegram → 打开 BotFather → /newbot 或 /mybots",
      "复制 Bot Token",
      "调用 setWebhook，将 url 设为本页 Webhook，将 secret_token 设为 TELEGRAM_WEBHOOK_TOKEN",
    ],
    fields: [
      { env: "TELEGRAM_BOT_TOKEN", label: "Bot Token", secret: true, placeholder: "123456:ABC", source: "BotFather → 机器人详情 → API token" },
      { env: "TELEGRAM_WEBHOOK_TOKEN", label: "Webhook Secret Token", secret: true, placeholder: "自定义高强度 token", source: "你生成的 1-256 位 secret_token，只允许字母、数字、下划线、短横线" },
      { env: "AGENT_HUB_PUBLIC_URL", label: "公网访问地址", placeholder: "https://agent.example.com", source: "你的服务器域名，Telegram Webhook 支持 443/80/88/8443" },
    ],
    steps: [
      "在 BotFather 创建机器人并取得 Bot Token。",
      "调用 Telegram setWebhook，把 secret_token 设置为 TELEGRAM_WEBHOOK_TOKEN。",
      "向机器人发送消息后，在任务页查看运行记录。",
    ],
    verify: ["setWebhook 返回 ok=true", "机器人收到文本后任务页出现新任务", "失败时检查证书、端口和 secret_token 请求头"],
  },
  slack: {
    purpose: "用于 Slack Events API。",
    auth: "Slack Signing Secret HMAC 签名",
    docUrl: "https://api.slack.com/apis/http",
    consoleUrl: "https://api.slack.com/apps",
    consolePath: [
      "Slack API → Your Apps → 选择应用 → Basic Information",
      "App Credentials → 复制 Signing Secret",
      "OAuth & Permissions → 复制 Bot User OAuth Token",
      "Event Subscriptions → Enable Events → Request URL 填本页 Webhook",
    ],
    fields: [
      { env: "SLACK_BOT_TOKEN", label: "Bot Token", secret: true, placeholder: "xoxb-...", source: "OAuth & Permissions → Bot User OAuth Token" },
      { env: "SLACK_SIGNING_SECRET", label: "Signing Secret", secret: true, placeholder: "Slack signing secret", source: "Basic Information → App Credentials → Signing Secret" },
    ],
    steps: [
      "在 Slack App 中启用 Event Subscriptions。",
      "Request URL 填写本页 Webhook 地址。",
      "订阅 message 相关事件后安装到 workspace 并发送测试消息。",
    ],
    verify: ["Slack Request URL 验证通过", "事件订阅保存成功", "频道中 @机器人 后任务页出现新任务"],
  },
  qq: {
    purpose: "用于 QQ 机器人或频道消息入口。",
    auth: "共享 Webhook Token",
    docUrl: "https://bot.q.qq.com/wiki/",
    consoleUrl: "https://q.qq.com/",
    consolePath: [
      "QQ 机器人平台 → 我的机器人/应用 → 选择机器人",
      "开发设置 → 复制 App ID / Token",
      "事件回调或 Webhook 配置 → 填写本页 Webhook 地址",
    ],
    fields: [
      { env: "QQ_BOT_APP_ID", label: "App ID", placeholder: "QQ bot app id", source: "QQ 机器人平台 → 开发设置 → App ID" },
      { env: "QQ_BOT_TOKEN", label: "Bot Token", secret: true, placeholder: "QQ bot token", source: "QQ 机器人平台 → 开发设置 → Token" },
      { env: "QQ_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token", source: "魔方 agent 自生成共享 token，用于保护入站 Webhook" },
    ],
    steps: [
      "在 QQ 机器人平台创建应用并启用事件。",
      "把事件回调地址设置为本页 Webhook。",
      "发送文本消息并检查运行详情中的归一化事件。",
    ],
    verify: ["平台回调保存成功", "QQ 侧发送文本消息", "任务页出现 QQ 来源的新任务"],
  },
  custom_webhook: {
    purpose: "用于接入其他支持 HTTP Webhook 的聊天软件或内部系统。",
    auth: "共享 Webhook Token",
    docUrl: "https://developer.mozilla.org/en-US/docs/Web/HTTP",
    consolePath: [
      "在第三方系统中找到 Webhook / Callback / Event Subscription 设置",
      "URL 填写本页 Webhook 地址",
      "Header 填 x-agent-hub-channel-token，值为 CUSTOM_WEBHOOK_TOKEN",
    ],
    fields: [
      { env: "CUSTOM_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token", source: "魔方 agent 自生成共享 token，第三方系统请求时放到请求头" },
    ],
    steps: [
      "调用本页 Webhook 地址，Body 使用 JSON 对象。",
      "请求头加入 x-agent-hub-channel-token，值为 CUSTOM_WEBHOOK_TOKEN。",
      "JSON 中至少包含 text 或 content 字段。",
    ],
    verify: ["curl 或第三方平台测试返回 2xx", "JSON 中的 text/content 进入任务页", "失败时查看响应体中的错误代码"],
  },
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status;
}

function channelRuntimeTitle(channel: ChannelStatus) {
  if (!channel.runtime) return null;
  if (channel.id === "feishu" && channel.runtime.status === "running") return "飞书长连接运行中";
  if (channel.id === "feishu" && channel.runtime.status === "starting") return "飞书长连接启动中";
  if (channel.id === "feishu" && channel.runtime.status === "not_started") return "飞书长连接未启动";
  if (channel.id === "feishu" && channel.runtime.status === "stopped") return "飞书长连接已停止";
  return `运行状态：${channel.runtime.status}`;
}

function channelRuntimeSummary(channel: ChannelStatus) {
  const runtime = channel.runtime;
  if (!runtime) return "";
  return `连接次数 ${runtime.connection_attempts} / 收到事件 ${runtime.received_events} / 已提交 ${runtime.submitted_messages} / 失败 ${runtime.failures}`;
}

function channelConfiguredSource(channel: ChannelStatus, env: string) {
  return channel.configured_sources[env] ?? (channel.configured.includes(env) ? "saved" : null);
}

function configuredSourceLabel(source: string | null) {
  if (source === "environment") return "服务器环境";
  if (source === "shared_saved") return "其他通道页面配置";
  if (source === "saved") return "本页保存";
  return "未配置";
}

function configuredPlaceholder(field: ChannelGuide["fields"][number], source: string | null) {
  if (source === "environment") return `服务器环境已配置，页面清空不会删除；如需临时覆盖请输入新的 ${field.label}`;
  if (source === "shared_saved") return `其他通道页面配置已提供，留空不覆盖；如需覆盖请输入新的 ${field.label}`;
  if (source === "saved") return `已配置，留空不覆盖；如需更换请输入新的 ${field.label}`;
  return field.placeholder;
}

function configuredSourceHelp(field: ChannelGuide["fields"][number], source: string | null) {
  if (!source) return null;
  return configuredPlaceholder(field, source);
}

function keepOptionLabel(source: string | null) {
  if (source === "environment") return "保持服务器环境配置";
  if (source === "shared_saved") return "保持其他通道页面配置";
  if (source === "saved") return "保持本页保存配置";
  return "选择配置";
}

function envTemplate(channel: ChannelStatus, guide: ChannelGuide) {
  return guide.fields
    .map((field) => {
      const source = channelConfiguredSource(channel, field.env);
      return `${field.env}=${source ? `<已配置：${configuredSourceLabel(source)}>` : field.placeholder}`;
    })
    .join("\n");
}

const FEISHU_RESOURCE_SELECTORS = [
  { command: "@github", description: "消息开头选择插件" },
  { command: "&research", description: "消息开头选择 Skill" },
  { command: "#filesystem", description: "消息开头选择 MCP" },
];

const FEISHU_RESOURCE_EXAMPLES = [
  "@github &research #filesystem 梳理这个仓库的改造计划",
  "&pdf 总结附件中的论文并给出后续研究方向",
  "请分析 @someone 的账号、#标题 和 C# 示例，不要当成资源调用",
];
type ChannelSortKey = "name" | "status" | "entry" | "missing";

type ChannelColumnFilters = {
  entry: string;
  missing: string;
  name: string;
  status: "all" | string;
};

const EMPTY_CHANNEL_FILTERS: ChannelColumnFilters = {
  entry: "",
  missing: "",
  name: "",
  status: "all",
};

function channelEntry(channel: ChannelStatus) {
  return channel.public_webhook_url ?? channel.webhook_path ?? "无";
}

function channelMissing(channel: ChannelStatus) {
  return channel.missing.length > 0 ? channel.missing.join(", ") : "无";
}

function channelSearchText(channel: ChannelStatus) {
  return [channel.name, channel.id, statusLabel(channel.status), channelEntry(channel), channelMissing(channel)].join(" ");
}

function matchesChannelColumns(channel: ChannelStatus, filters: ChannelColumnFilters) {
  return (
    textContains(`${channel.name} ${channel.id}`, filters.name) &&
    (filters.status === "all" || channel.status === filters.status) &&
    textContains(channelEntry(channel), filters.entry) &&
    textContains(channelMissing(channel), filters.missing)
  );
}

function sortedChannels(channels: ChannelStatus[], sort: SortState<ChannelSortKey>) {
  const copy = [...channels];
  return copy.sort((left, right) => {
    let result = 0;
    if (sort.key === "name") result = compareText(left.name, right.name, "asc");
    if (sort.key === "status") result = compareText(statusLabel(left.status), statusLabel(right.status), "asc");
    if (sort.key === "entry") result = compareText(channelEntry(left), channelEntry(right), "asc");
    if (sort.key === "missing") result = compareText(channelMissing(left), channelMissing(right), "asc");
    return sort.direction === "asc" ? result : -result;
  });
}

function mergeChannelStatus(channels: ChannelStatus[] | undefined, status: ChannelStatus) {
  if (!channels || channels.length === 0) return [status];
  let matched = false;
  const next = channels.map((channel) => {
    if (channel.id !== status.id) return channel;
    matched = true;
    return status;
  });
  return matched ? next : [status, ...next];
}
export function ChannelsPage() {
  const queryClient = useQueryClient();
  const { activeSection, navTargetProps } = useNavSection(["provider", "section", "mode"]);
  const channels = useQuery({ queryKey: ["channels"], queryFn: () => api.channels() });
  const [selectedId, setSelectedId] = useState("feishu");
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const [channelSearchTerm, setChannelSearchTerm] = useState("");
  const [channelColumnFilters, setChannelColumnFilters] = useState<ChannelColumnFilters>(EMPTY_CHANNEL_FILTERS);
  const [channelSort, setChannelSort] = useState<SortState<ChannelSortKey>>({ key: "name", direction: "asc" });

  const items = channels.data ?? [];
  const selected = useMemo(() => items.find((item) => item.id === selectedId) ?? items[0] ?? null, [items, selectedId]);
  const guide = selected ? CHANNEL_GUIDES[selected.id] : null;

  useEffect(() => {
    if (activeSection && items.some((channel) => channel.id === activeSection)) setSelectedId(activeSection);
  }, [activeSection, items]);

  useEffect(() => {
    setDraftValues({});
    setSaveNotice(null);
  }, [selectedId]);

  const saveConfig = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("channel is not selected");
      return api.saveChannelConfig(selected.id, { values: draftValues });
    },
    onSuccess: async (data) => {
      setDraftValues({});
      queryClient.setQueryData<ChannelStatus[]>(["channels"], (current) => mergeChannelStatus(current, data.status));
      setSaveNotice("通道配置已保存，可继续修改或清空。面板已刷新最新状态。");
      await queryClient.invalidateQueries({ queryKey: ["channels"] });
    },
  });

  const clearConfig = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("channel is not selected");
      return api.clearChannelConfig(selected.id);
    },
    onSuccess: async (data) => {
      setDraftValues({});
      queryClient.setQueryData<ChannelStatus[]>(["channels"], (current) => mergeChannelStatus(current, data.status));
      const remainingSources = Object.values(data.status.configured_sources);
      setSaveNotice(
        remainingSources.includes("environment")
          ? "本页保存的通道配置已清空；服务器环境变量仍会继续生效。"
          : remainingSources.includes("shared_saved")
            ? "本页保存的通道配置已清空；其他通道页面配置仍会继续生效。"
            : "通道配置已清空。需要重新填写后才会接通。",
      );
      await queryClient.invalidateQueries({ queryKey: ["channels"] });
    },
  });

  if (channels.isLoading) return <p>正在加载通道状态...</p>;
  if (channels.isError) {
    return <p role="alert">{formatApiError(channels.error, "通道状态加载失败")}</p>;
  }
  const visibleChannels = sortedChannels(
    items.filter((channel) => textContains(channelSearchText(channel), channelSearchTerm) && matchesChannelColumns(channel, channelColumnFilters)),
    channelSort,
  );

  function updateChannelColumnFilter<Key extends keyof ChannelColumnFilters>(key: Key, value: ChannelColumnFilters[Key]) {
    setChannelColumnFilters((current) => ({ ...current, [key]: value }));
  }

  return (
    <section>
      <p className="eyebrow">Channel hub</p>
      <h2>通道连接</h2>
      <p>
        通道用于把飞书、钉钉、企业微信、微信、Telegram、Slack、QQ 或自定义 Webhook
        的消息连接到主 Agent。每个通道本质上都是“把一个 Agent 接到聊天入口”，底层平台凭证只用于验签、收消息和回复。
        校验失败会返回明确错误。
      </p>

      <div className="channel-console">
        <aside className="channel-picker" aria-label="选择接入通道">
          <h3>选择要连接 Agent 的通道</h3>
          {items.map((channel) => (
            <button
              key={channel.id}
              type="button"
              className={`channel-option${selected?.id === channel.id ? " channel-option-active" : ""}`}
              onClick={() => setSelectedId(channel.id)}
            >
              <span>{channel.name}</span>
              <small>{statusLabel(channel.status)}</small>
            </button>
          ))}
        </aside>

        {selected && guide ? (
          <div className="channel-detail">
            <article {...navTargetProps(selected.id)}>
              <span className="eyebrow">{selected.id}</span>
              <h3>{selected.name}</h3>
              <p>{guide.purpose}</p>
              <div className="detail-grid">
                <div>
                  <span className="eyebrow">当前状态</span>
                  <p>{statusLabel(selected.status)}</p>
                </div>
                <div>
                  <span className="eyebrow">校验方式</span>
                  <p>{guide.auth}</p>
                </div>
                <div>
                  <span className="eyebrow">Webhook</span>
                  <p className="code-line">{selected.public_webhook_url ?? selected.webhook_path ?? "无"}</p>
                </div>
                <div>
                  <span className="eyebrow">传输方式</span>
                  <p>{selected.transports.join(" / ") || "待定义"}</p>
                </div>
              </div>
              {selected.runtime ? (
                <div className="channel-runtime-status" role="status">
                  <strong>{channelRuntimeTitle(selected)}</strong>
                  <span>{channelRuntimeSummary(selected)}</span>
                  {selected.runtime.last_error_type ? (
                    <span>
                      最近错误：{selected.runtime.last_error_type}
                      {selected.runtime.last_error_message ? ` - ${selected.runtime.last_error_message}` : ""}
                    </span>
                  ) : null}
                </div>
              ) : null}
              {selected.missing.length > 0 ? (
                <p role="alert">还缺少配置：{selected.missing.join(", ")}</p>
              ) : (
                <p role="status">必要配置已齐全。可以按接入步骤发送测试消息。</p>
              )}
            </article>

            {selected.id === "feishu" ? (
              <section {...navTargetProps("resources", "channel-command-guide")} aria-label="飞书资源选择器">
                <h3>资源选择器</h3>
                <p>飞书消息默认先交给主 Agent 判断入口、模式和是否需要计划、OpenClaw 或 Vibe Coding。需要指定资源时，只在消息开头连续写资源选择器；正文开始后出现的 @、&、# 不会被当成调用。</p>
                <div className="channel-command-grid">
                  {FEISHU_RESOURCE_SELECTORS.map((item) => (
                    <div key={item.command}>
                      <code>{item.command}</code>
                      <span>{item.description}</span>
                    </div>
                  ))}
                </div>
                <div className="channel-command-examples">
                  <span className="eyebrow">示例</span>
                  {FEISHU_RESOURCE_EXAMPLES.map((example) => (
                    <code key={example}>{example}</code>
                  ))}
                </div>
                <p className="field-help">通道不会再强制选择运行模式；如果已经在 Web 或会话中选择了模式，后续消息会沿用主 Agent 的入口判断，不需要反复选择。</p>
              </section>
            ) : null}
            <article>
              <h3>官方入口与点击路径</h3>
              <div className="link-actions">
                <a href={guide.docUrl} target="_blank" rel="noreferrer">
                  打开{selected.name}官方文档
                </a>
                {guide.consoleUrl ? (
                  <a href={guide.consoleUrl} target="_blank" rel="noreferrer">
                    打开{selected.name}控制台
                  </a>
                ) : null}
              </div>
              <ol className="compact-list">
                {guide.consolePath.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
            </article>

            <article {...navTargetProps("reply")}>
              <h3>配置内容</h3>
              <p className="field-help">
可直接在这里填写并保存；已配置的密钥不会回显。输入新值会覆盖旧配置，留空不会修改已有配置。需要重新接入时可以清空本页保存的通道配置。
              </p>
              <div className="form-grid">
                {guide.fields.map((field) => {
                  const source = channelConfiguredSource(selected, field.env);
                  return (
                    <label key={field.env}>
                      {field.label}
                      {field.options ? (
                        <select
                          value={draftValues[field.env] ?? ""}
                          onChange={(event) =>
                            setDraftValues((current) => ({
                              ...current,
                              [field.env]: event.target.value,
                            }))
                          }
                        >
                          <option value="">{keepOptionLabel(source)}</option>
                          {field.options.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          type={field.secret ? "password" : "text"}
                          autoComplete="off"
                          value={draftValues[field.env] ?? ""}
                          onChange={(event) =>
                            setDraftValues((current) => ({
                              ...current,
                              [field.env]: event.target.value,
                            }))
                          }
                          placeholder={configuredPlaceholder(field, source)}
                        />
                      )}
                      <span className="field-help">{field.env}</span>
                      {source ? <span className="field-help">当前来源：{configuredSourceLabel(source)}</span> : null}
                      {configuredSourceHelp(field, source) ? (
                        <span className="field-help">{configuredSourceHelp(field, source)}</span>
                      ) : null}
                      <span className="field-help">来源：{field.source}</span>
                    </label>
                  );
                })}
              </div>
              <div className="channel-config-actions" role="group" aria-label="通道配置操作">
                <button
                  type="button"
                  disabled={saveConfig.isPending || Object.values(draftValues).every((value) => value.trim() === "")}
                  onClick={() => saveConfig.mutate()}
                >
                  {saveConfig.isPending ? "保存中..." : "保存通道配置"}
                </button>
                <button
                  type="button"
                  className="danger-action"
                  disabled={clearConfig.isPending || selected.status !== "configured"}
                  onClick={() => clearConfig.mutate()}
                >
                  {clearConfig.isPending ? "清空中..." : "清空当前通道配置"}
                </button>
              </div>
              {saveNotice ? <p className="channel-config-status" role="status">{saveNotice}</p> : null}
              {saveConfig.isError ? (
                <p className="form-error" role="alert">
                  {formatApiError(saveConfig.error, "通道配置保存失败")}
                </p>
              ) : null}
              {clearConfig.isError ? (
                <p className="form-error" role="alert">
                  {formatApiError(clearConfig.error, "通道配置清空失败")}
                </p>
              ) : null}
            </article>

            <article>
              <h3>部署配置模板</h3>
              <p>这里用于核对部署变量；在本页保存会自动刷新运行中配置，只有手工改服务器环境文件时才需要重启服务。</p>
              <pre className="code-block">{envTemplate(selected, guide)}</pre>
            </article>

            <article>
              <h3>接入步骤</h3>
              <ol>
                {guide.steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
              <h4>验证方式</h4>
              <ol className="compact-list">
                {guide.verify.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ol>
              <p>{selected.notes.join(" ")}</p>
            </article>
          </div>
        ) : null}
      </div>

      <section aria-label="通道支持矩阵" {...navTargetProps("test")}>
        <h3>通道支持矩阵</h3>
        <div className="list-toolbar">
          <label>
            快速搜索通道
            <input
              type="search"
              aria-label="快速搜索通道"
              value={channelSearchTerm}
              onChange={(event) => setChannelSearchTerm(event.currentTarget.value)}
              placeholder="通道、状态、入口或缺失配置"
            />
          </label>
          <button type="button" className="secondary-action" onClick={() => { setChannelSearchTerm(""); setChannelColumnFilters(EMPTY_CHANNEL_FILTERS); }}>
            清空筛选
          </button>
        </div>
        {visibleChannels.length === 0 ? (
          <article>
            <h4>当前筛选没有匹配通道</h4>
            <p>调整列筛选或清空筛选查看全部通道。</p>
          </article>
        ) : (
          <table aria-label="通道支持矩阵列表">
            <thead>
              <tr>
                <th><SortHeader column="name" label="通道" sort={channelSort} onSort={(column) => setChannelSort((current) => nextSortState(current, column))}>通道</SortHeader></th>
                <th><SortHeader column="status" label="状态" sort={channelSort} onSort={(column) => setChannelSort((current) => nextSortState(current, column))}>状态</SortHeader></th>
                <th><SortHeader column="entry" label="入口" sort={channelSort} onSort={(column) => setChannelSort((current) => nextSortState(current, column))}>入口</SortHeader></th>
                <th><SortHeader column="missing" label="缺失配置" sort={channelSort} onSort={(column) => setChannelSort((current) => nextSortState(current, column))}>缺失配置</SortHeader></th>
              </tr>
              <tr className="table-filter-row">
                <th><input aria-label="按通道筛选" value={channelColumnFilters.name} onChange={(event) => updateChannelColumnFilter("name", event.currentTarget.value)} placeholder="名称或 ID" /></th>
                <th>
                  <select aria-label="按通道状态筛选" value={channelColumnFilters.status} onChange={(event) => updateChannelColumnFilter("status", event.currentTarget.value)}>
                    <option value="all">全部</option>
                    <option value="configured">已接通</option>
                    <option value="missing_config">待配置</option>
                  </select>
                </th>
                <th><input aria-label="按通道入口筛选" value={channelColumnFilters.entry} onChange={(event) => updateChannelColumnFilter("entry", event.currentTarget.value)} placeholder="Webhook 或路径" /></th>
                <th><input aria-label="按缺失配置筛选" value={channelColumnFilters.missing} onChange={(event) => updateChannelColumnFilter("missing", event.currentTarget.value)} placeholder="环境变量或无" /></th>
              </tr>
            </thead>
            <tbody>
              {visibleChannels.map((channel) => (
                <tr key={channel.id}>
                  <td>
                    <strong>{channel.name}</strong>
                    <br />
                    <span>{channel.id}</span>
                  </td>
                  <td>{statusLabel(channel.status)}</td>
                  <td>{channelEntry(channel)}</td>
                  <td>{channelMissing(channel)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}
