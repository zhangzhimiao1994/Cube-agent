import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, formatApiError, type ChannelStatus } from "../api/client";

const STATUS_LABELS: Record<string, string> = {
  configured: "已接通",
  missing_config: "待配置",
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status;
}

function channelSummary(channel: ChannelStatus) {
  if (channel.status === "configured") return "环境变量齐全，后端已可接收该通道事件。";
  if (channel.status === "missing_config") return "已列入通道矩阵，但还缺少必要配置。";
  return "状态未知，请检查后端通道配置。";
}

export function ChannelsPage() {
  const channels = useQuery({ queryKey: ["channels"], queryFn: () => api.channels() });
  const [selectedId, setSelectedId] = useState("feishu");

  const selected = useMemo(() => {
    const items = channels.data ?? [];
    return items.find((item) => item.id === selectedId) ?? items[0] ?? null;
  }, [channels.data, selectedId]);

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
        这里集中管理聊天软件入口。每个通道都有独立 Webhook，配置齐全并通过令牌校验后，
        消息会归一化提交到主 Agent 的运行队列。
      </p>

      <article>
        <h3>选择要接入的通道</h3>
        <label htmlFor="channel-select">通道</label>
        <select
          id="channel-select"
          value={selected?.id ?? ""}
          onChange={(event) => setSelectedId(event.target.value)}
        >
          {items.map((channel) => (
            <option key={channel.id} value={channel.id}>
              {channel.name} ({channel.id})
            </option>
          ))}
        </select>

        {selected ? (
          <div className="detail-grid">
            <div>
              <span className="eyebrow">当前状态</span>
              <h3>{selected.name}</h3>
              <p>{statusLabel(selected.status)}</p>
              <p>{channelSummary(selected)}</p>
            </div>
            <div>
              <span className="eyebrow">Webhook</span>
              <p>{selected.public_webhook_url ?? selected.webhook_path ?? "该通道暂不需要 Webhook"}</p>
            </div>
            <div>
              <span className="eyebrow">传输方式</span>
              <p>{selected.transports.join(" / ") || "待定义"}</p>
            </div>
            <div>
              <span className="eyebrow">缺失配置</span>
              <p>{selected.missing.length > 0 ? selected.missing.join(", ") : "无"}</p>
            </div>
          </div>
        ) : null}
      </article>

      <section aria-label="通道支持矩阵">
        <h3>通道支持矩阵</h3>
        <table>
          <thead>
            <tr>
              <th>通道</th>
              <th>状态</th>
              <th>入口</th>
              <th>缺失配置</th>
              <th>说明</th>
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
                <td>{channel.notes.join(" ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <article>
        <h3>配置指引</h3>
        <p>
          对飞书，把事件订阅 Request URL 设置为上方 Webhook 地址，并在服务器环境变量里配置
          FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_VERIFICATION_TOKEN、FEISHU_ENCRYPT_KEY。
          其他平台按表格里的缺失变量配置对应令牌和平台参数；缺失配置会返回明确错误，不会静默失败。
        </p>
      </article>
    </section>
  );
}
