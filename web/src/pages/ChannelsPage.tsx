import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, formatApiError, type ChannelStatus } from "../api/client";

type ChannelGuide = {
  purpose: string;
  auth: string;
  fields: { env: string; label: string; secret?: boolean; placeholder: string }[];
  steps: string[];
};

const STATUS_LABELS: Record<string, string> = {
  configured: "已接通",
  missing_config: "待配置",
};

const CHANNEL_GUIDES: Record<string, ChannelGuide> = {
  feishu: {
    purpose: "用于飞书机器人、群聊和私聊入口，支持事件订阅进入主 Agent。",
    auth: "飞书签名 + verification token + encrypt key",
    fields: [
      { env: "FEISHU_APP_ID", label: "App ID", placeholder: "cli_xxx" },
      { env: "FEISHU_APP_SECRET", label: "App Secret", secret: true, placeholder: "飞书应用密钥" },
      { env: "FEISHU_VERIFICATION_TOKEN", label: "Verification Token", secret: true, placeholder: "事件订阅校验 token" },
      { env: "FEISHU_ENCRYPT_KEY", label: "Encrypt Key", secret: true, placeholder: "事件加密 key" },
      { env: "AGENT_HUB_PUBLIC_URL", label: "公网访问地址", placeholder: "https://agent.example.com" },
    ],
    steps: [
      "在飞书开放平台创建企业自建应用并启用机器人能力。",
      "把事件订阅 Request URL 设置为本页 Webhook 地址。",
      "把本页列出的环境变量写入服务器配置后重启 API 服务。",
      "在飞书里给机器人发送一条文本消息，任务页应出现新运行记录。",
    ],
  },
  dingtalk: {
    purpose: "用于钉钉机器人或应用事件入口。",
    auth: "共享 Webhook Token，服务端校验 x-agent-hub-channel-token 或 token 参数",
    fields: [
      { env: "DINGTALK_APP_KEY", label: "App Key", placeholder: "ding app key" },
      { env: "DINGTALK_APP_SECRET", label: "App Secret", secret: true, placeholder: "ding app secret" },
      { env: "DINGTALK_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token" },
    ],
    steps: [
      "在钉钉开放平台或机器人配置中创建事件回调。",
      "Webhook 指向本页地址，并带上配置的 token。",
      "发送文本事件后，到任务页查看是否创建运行记录。",
    ],
  },
  wecom_bot: {
    purpose: "用于企业微信群智能机器人。",
    auth: "共享 Webhook Token",
    fields: [
      { env: "WECOM_BOT_WEBHOOK_KEY", label: "Webhook Key", secret: true, placeholder: "企微机器人 key" },
      { env: "WECOM_BOT_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token" },
    ],
    steps: [
      "在企业微信群添加机器人并取得 Webhook Key。",
      "把回调地址设置为本页 Webhook，并配置共享 token。",
      "在群内发送测试文本，检查任务页和运行详情。",
    ],
  },
  wecom_app: {
    purpose: "用于企业微信自建应用，适合内部私聊机器人和审批入口。",
    auth: "企业微信 SHA1 回调签名",
    fields: [
      { env: "WECOM_CORP_ID", label: "Corp ID", placeholder: "wwxxxx" },
      { env: "WECOM_AGENT_ID", label: "Agent ID", placeholder: "1000002" },
      { env: "WECOM_SECRET", label: "Secret", secret: true, placeholder: "自建应用 secret" },
      { env: "WECOM_TOKEN", label: "Token", secret: true, placeholder: "回调 token" },
    ],
    steps: [
      "在企业微信管理后台创建自建应用。",
      "把接收消息服务器 URL 设置为本页 Webhook。",
      "保存 token 和 secret 后重启服务，并用企业微信发送文本消息测试。",
    ],
  },
  wechat_official: {
    purpose: "用于微信公众号消息入口。",
    auth: "微信 SHA1 回调签名",
    fields: [
      { env: "WECHATMP_APP_ID", label: "App ID", placeholder: "公众号 app id" },
      { env: "WECHATMP_APP_SECRET", label: "App Secret", secret: true, placeholder: "公众号 app secret" },
      { env: "WECHATMP_TOKEN", label: "Token", secret: true, placeholder: "服务器配置 token" },
    ],
    steps: [
      "在公众号后台启用服务器配置。",
      "URL 填本页 Webhook，Token 填环境变量中的 WECHATMP_TOKEN。",
      "关注公众号后发送文本消息，到任务页核验。",
    ],
  },
  wechat_customer_service: {
    purpose: "用于微信客服入口。",
    auth: "微信客服 SHA1 回调签名",
    fields: [
      { env: "WECHAT_KF_CORP_ID", label: "Corp ID", placeholder: "企业 ID" },
      { env: "WECHAT_KF_SECRET", label: "Secret", secret: true, placeholder: "客服 secret" },
      { env: "WECHAT_KF_TOKEN", label: "Token", secret: true, placeholder: "回调 token" },
    ],
    steps: [
      "在微信客服后台配置回调地址。",
      "按本页字段配置企业 ID、Secret 和 Token。",
      "通过客服入口发送测试文本，检查任务是否进入队列。",
    ],
  },
  telegram: {
    purpose: "用于 Telegram Bot Webhook。",
    auth: "Telegram Secret Token 请求头",
    fields: [
      { env: "TELEGRAM_BOT_TOKEN", label: "Bot Token", secret: true, placeholder: "123456:ABC" },
      { env: "TELEGRAM_WEBHOOK_TOKEN", label: "Webhook Secret Token", secret: true, placeholder: "自定义高强度 token" },
      { env: "AGENT_HUB_PUBLIC_URL", label: "公网访问地址", placeholder: "https://agent.example.com" },
    ],
    steps: [
      "在 BotFather 创建机器人并取得 Bot Token。",
      "调用 Telegram setWebhook，把 secret_token 设置为 TELEGRAM_WEBHOOK_TOKEN。",
      "向机器人发送消息后，在任务页查看运行记录。",
    ],
  },
  slack: {
    purpose: "用于 Slack Events API。",
    auth: "Slack Signing Secret HMAC 签名",
    fields: [
      { env: "SLACK_BOT_TOKEN", label: "Bot Token", secret: true, placeholder: "xoxb-..." },
      { env: "SLACK_SIGNING_SECRET", label: "Signing Secret", secret: true, placeholder: "Slack signing secret" },
    ],
    steps: [
      "在 Slack App 中启用 Event Subscriptions。",
      "Request URL 填写本页 Webhook 地址。",
      "订阅 message 相关事件后安装到 workspace 并发送测试消息。",
    ],
  },
  qq: {
    purpose: "用于 QQ 机器人或频道消息入口。",
    auth: "共享 Webhook Token",
    fields: [
      { env: "QQ_BOT_APP_ID", label: "App ID", placeholder: "QQ bot app id" },
      { env: "QQ_BOT_TOKEN", label: "Bot Token", secret: true, placeholder: "QQ bot token" },
      { env: "QQ_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token" },
    ],
    steps: [
      "在 QQ 机器人平台创建应用并启用事件。",
      "把事件回调地址设置为本页 Webhook。",
      "发送文本消息并检查运行详情中的归一化事件。",
    ],
  },
  custom_webhook: {
    purpose: "用于接入其他支持 HTTP Webhook 的聊天软件或内部系统。",
    auth: "共享 Webhook Token",
    fields: [
      { env: "CUSTOM_WEBHOOK_TOKEN", label: "Webhook Token", secret: true, placeholder: "自定义高强度 token" },
    ],
    steps: [
      "调用本页 Webhook 地址，Body 使用 JSON 对象。",
      "请求头加入 x-agent-hub-channel-token，值为 CUSTOM_WEBHOOK_TOKEN。",
      "JSON 中至少包含 text 或 content 字段。",
    ],
  },
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status;
}

function envTemplate(channel: ChannelStatus, guide: ChannelGuide) {
  return guide.fields
    .map((field) => `${field.env}=${channel.missing.includes(field.env) ? field.placeholder : "<已配置>"}`)
    .join("\n");
}

export function ChannelsPage() {
  const channels = useQuery({ queryKey: ["channels"], queryFn: () => api.channels() });
  const [selectedId, setSelectedId] = useState("feishu");

  const selected = useMemo(() => {
    const items = channels.data ?? [];
    return items.find((item) => item.id === selectedId) ?? items[0] ?? null;
  }, [channels.data, selectedId]);
  const guide = selected ? CHANNEL_GUIDES[selected.id] : null;

  if (channels.isLoading) return <p>正在加载通道状态...</p>;
  if (channels.isError) {
    return <p role="alert">{formatApiError(channels.error, "通道状态加载失败")}</p>;
  }

  const items = channels.data ?? [];

  return (
    <section>
      <p className="eyebrow">Channel hub</p>
      <h2>通道连接</h2>
      <p>
        通道用于把飞书、钉钉、企业微信、微信、Telegram、Slack、QQ 或自定义 Webhook
        的消息接入主 Agent。每个通道都会做平台校验，校验失败会返回明确错误。
      </p>

      <div className="channel-console">
        <aside className="channel-picker" aria-label="选择接入通道">
          <h3>选择要接入的通道</h3>
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
            <article>
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
              {selected.missing.length > 0 ? (
                <p role="alert">还缺少配置：{selected.missing.join(", ")}</p>
              ) : (
                <p role="status">必要配置已齐全。可以按接入步骤发送测试消息。</p>
              )}
            </article>

            <article>
              <h3>配置内容</h3>
              <div className="form-grid">
                {guide.fields.map((field) => (
                  <label key={field.env}>
                    {field.label}
                    <input
                      readOnly
                      type={field.secret ? "password" : "text"}
                      value={selected.missing.includes(field.env) ? "" : "<已配置>"}
                      placeholder={field.placeholder}
                    />
                    <span className="field-help">{field.env}</span>
                  </label>
                ))}
              </div>
            </article>

            <article>
              <h3>部署配置模板</h3>
              <p>把下面变量写入服务器环境配置或安装脚本生成的 `.env`，然后重启服务。</p>
              <pre className="code-block">{envTemplate(selected, guide)}</pre>
            </article>

            <article>
              <h3>接入步骤</h3>
              <ol>
                {guide.steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
              <p>{selected.notes.join(" ")}</p>
            </article>
          </div>
        ) : null}
      </div>

      <section aria-label="通道支持矩阵">
        <h3>通道支持矩阵</h3>
        <table>
          <thead>
            <tr>
              <th>通道</th>
              <th>状态</th>
              <th>入口</th>
              <th>缺失配置</th>
            </tr>
          </thead>
          <tbody>
            {items.map((channel) => (
              <tr key={channel.id}>
                <td>
                  <strong>{channel.name}</strong>
                  <br />
                  <span>{channel.id}</span>
                </td>
                <td>{statusLabel(channel.status)}</td>
                <td>{channel.public_webhook_url ?? channel.webhook_path ?? "无"}</td>
                <td>{channel.missing.length > 0 ? channel.missing.join(", ") : "无"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </section>
  );
}
