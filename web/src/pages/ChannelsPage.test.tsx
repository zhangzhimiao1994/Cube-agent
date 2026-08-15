import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const principal = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  role: "super_admin",
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const channelPayload = [
  {
    id: "feishu",
    name: "飞书",
    status: "configured",
    transports: ["webhook"],
    webhook_path: "/channels/feishu/events",
    public_webhook_url: "https://agent.example.com/channels/feishu/events",
    configured: ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TRANSPORT"],
    missing: [],
    notes: ["Webhook 已挂载在主 API 服务，不需要额外暴露 8001。"],
    runtime: {
      status: "running",
      ready: true,
      connection_attempts: 2,
      reconnects: 1,
      received_events: 3,
      submitted_messages: 2,
      ignored_events: 1,
      failures: 0,
      last_error_type: null,
      last_error_message: null,
    },
  },
  {
    id: "dingtalk",
    name: "钉钉",
    status: "missing_config",
    transports: ["webhook"],
    webhook_path: "/channels/dingtalk/events",
    public_webhook_url: "https://agent.example.com/channels/dingtalk/events",
    missing: ["DINGTALK_APP_KEY"],
    configured: [],
    notes: ["已列入通道配置矩阵；真实验签和消息归一化适配器需要按平台规范接线。"],
  },
  {
    id: "wecom_bot",
    name: "企微智能机器人",
    status: "missing_config",
    transports: ["webhook"],
    webhook_path: "/channels/wecom/bot/events",
    public_webhook_url: "https://agent.example.com/channels/wecom/bot/events",
    missing: ["WECOM_BOT_WEBHOOK_KEY"],
    configured: [],
    notes: ["适合企业微信群机器人场景；需配置机器人 webhook key 后再启用。"],
  },
  {
    id: "wecom_app",
    name: "企业微信 Agent",
    status: "missing_config",
    transports: ["callback"],
    webhook_path: "/channels/wecom/app/events",
    public_webhook_url: "https://agent.example.com/channels/wecom/app/events",
    missing: ["WECOM_CORP_ID"],
    configured: [],
    notes: ["适合企业内部审批、任务派发和私聊机器人。"],
  },
  {
    id: "wechat_official",
    name: "公众号",
    status: "missing_config",
    transports: ["callback"],
    webhook_path: "/channels/wechatmp/events",
    public_webhook_url: "https://agent.example.com/channels/wechatmp/events",
    missing: ["WECHATMP_APP_ID"],
    configured: [],
    notes: ["适合公众号消息入口；配置齐全后可接收文本消息。"],
  },
  {
    id: "wechat_customer_service",
    name: "微信客服",
    status: "missing_config",
    transports: ["callback"],
    webhook_path: "/channels/wechat-kf/events",
    public_webhook_url: "https://agent.example.com/channels/wechat-kf/events",
    missing: ["WECHAT_KF_CORP_ID"],
    configured: [],
    notes: ["适合微信客服入口；配置齐全后可接收客服消息。"],
  },
  {
    id: "telegram",
    name: "Telegram",
    status: "missing_config",
    transports: ["webhook"],
    webhook_path: "/channels/telegram/events",
    public_webhook_url: "https://agent.example.com/channels/telegram/events",
    missing: ["TELEGRAM_BOT_TOKEN"],
    configured: [],
    notes: ["适合海外聊天机器人场景；需配置 Bot Token 和 webhook。"],
  },
  {
    id: "slack",
    name: "Slack",
    status: "missing_config",
    transports: ["events_api"],
    webhook_path: "/channels/slack/events",
    public_webhook_url: "https://agent.example.com/channels/slack/events",
    missing: ["SLACK_BOT_TOKEN"],
    configured: [],
    notes: ["适合团队协作空间；需校验 signing secret。"],
  },
  {
    id: "qq",
    name: "QQ 机器人",
    status: "missing_config",
    transports: ["webhook"],
    webhook_path: "/channels/qq/events",
    public_webhook_url: "https://agent.example.com/channels/qq/events",
    missing: ["QQ_BOT_APP_ID"],
    configured: [],
    notes: ["适合 QQ 频道或机器人入口；需按平台事件格式接线。"],
  },
  {
    id: "custom_webhook",
    name: "自定义 Webhook",
    status: "configured",
    transports: ["webhook"],
    webhook_path: "/channels/custom/events",
    public_webhook_url: "https://agent.example.com/channels/custom/events",
    missing: [],
    notes: ["用于兼容其他支持 HTTP Webhook 的聊天软件；配置共享令牌后可接收 JSON 文本消息。"],
  },
];

describe("ChannelsPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  let currentChannels: Array<Record<string, unknown>> = channelPayload;

  beforeEach(() => {
    requests.length = 0;
    currentChannels = channelPayload;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        requests.push({
          path,
          method,
          body: init?.body && typeof init.body === "string" ? JSON.parse(init.body) : null,
        });
        if (path === "/api/v1/auth/me") return jsonResponse(principal);
        if (path === "/api/v1/admin/channels") return jsonResponse(currentChannels);
        if (path === "/api/v1/admin/channels/dingtalk/config" && method === "POST") {
          return jsonResponse({
            id: "dingtalk",
            saved: ["DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_WEBHOOK_TOKEN"],
            status: {
              ...channelPayload[1],
              status: "configured",
              missing: [],
              configured: ["DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_WEBHOOK_TOKEN"],
            },
          });
        }
        if (path === "/api/v1/admin/channels/feishu/config" && method === "DELETE") {
          const clearedFeishu = {
            ...channelPayload[0],
            status: "missing_config",
            configured: [],
            configured_sources: {},
            missing: ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
            runtime: {
              status: "not_started",
              ready: false,
              connection_attempts: 0,
              reconnects: 0,
              received_events: 0,
              submitted_messages: 0,
              ignored_events: 0,
              failures: 0,
              last_error_type: null,
              last_error_message: null,
            },
          };
          currentChannels = [clearedFeishu, ...channelPayload.slice(1)];
          return jsonResponse({ id: "feishu", saved: [], status: clearedFeishu });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("shows channel connection status and setup guidance", async () => {
    render(<TestApp initialPath="/channels" />);

    expect(await screen.findByRole("heading", { name: "通道连接" })).not.toBeNull();
    expect(screen.getByLabelText("选择接入通道")).not.toBeNull();
    expect(
      screen.getAllByText("https://agent.example.com/channels/feishu/events").length,
    ).toBeGreaterThan(0);
    for (const name of [
      "飞书",
      "钉钉",
      "企微智能机器人",
      "企业微信 Agent",
      "公众号",
      "微信客服",
      "Telegram",
      "Slack",
      "QQ 机器人",
      "自定义 Webhook",
    ]) {
      expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("已接通").length).toBeGreaterThan(0);
    expect(screen.getAllByText("待配置").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "配置内容" })).not.toBeNull();
    expect(screen.getByRole("heading", { name: "部署配置模板" })).not.toBeNull();
    expect(screen.getAllByText(/消息连接到主 Agent/).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "选择要连接 Agent 的通道" })).not.toBeNull();
    expect(screen.queryByText(/企业微信自建应用/)).toBeNull();
    expect(screen.getByRole("link", { name: "打开飞书官方文档" }).getAttribute("href")).toBe(
      "https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?lang=zh-CN",
    );
    expect(screen.getByRole("link", { name: "打开飞书控制台" }).getAttribute("href")).toBe(
      "https://open.feishu.cn/app",
    );
    expect(screen.getByText("开发者后台 → 我的应用 → 选择机器人入口对应的应用 → 凭证与基础信息")).not.toBeNull();
    expect(screen.getByRole("combobox", { name: /应用类型/ })).not.toBeNull();
    expect(screen.getByRole("combobox", { name: /接收方式/ })).not.toBeNull();
    expect(screen.getByRole("option", { name: "机器人模板应用" })).not.toBeNull();
    expect(screen.getByText("来源：凭证与基础信息 → App ID；机器人模板应用也会提供")).not.toBeNull();
    expect(screen.getByRole("option", { name: "长连接" })).not.toBeNull();
    expect(screen.getByText(/长连接与 CowAgent\/OpenClaw 一样只需要 App ID/)).not.toBeNull();
    expect(screen.getByText(/校验失败会返回明确错误/)).not.toBeNull();
    expect(screen.getByText("飞书长连接运行中")).not.toBeNull();
    expect(screen.getByText("连接次数 2 / 收到事件 3 / 已提交 2 / 失败 0")).not.toBeNull();
    expect(screen.getByRole("textbox", { name: /交互指令别名 FEISHU_COMMAND_ALIASES/ })).not.toBeNull();
    expect(screen.getByText(/标准指令支持 \/\/自动、\/\/直连、\/\/派单/)).not.toBeNull();
    expect(screen.getByRole("region", { name: "飞书通道交互指令" })).not.toBeNull();
    expect(screen.getByText("//帮助")).not.toBeNull();
    expect(screen.getByText(/发送“帮助”“菜单”“指令”或“\/\/帮助”/)).not.toBeNull();
    expect(screen.getByText(/方案=\/\/派单, 代码=\/\/vi, 菜单=\/\/帮助/)).not.toBeNull();
  });

  it("does not mark unconfigured optional channel fields as already configured", async () => {
    render(<TestApp initialPath="/channels" />);

    await screen.findByRole("heading", { name: "通道连接" });

    expect(screen.getByLabelText(/Verification Token/).getAttribute("placeholder")).toBe(
      "自建应用事件订阅校验 token",
    );
    expect(screen.getByLabelText(/Encrypt Key/).getAttribute("placeholder")).toBe(
      "启用事件加密时填写",
    );
    expect(screen.getByRole("textbox", { name: /App ID FEISHU_APP_ID/ }).getAttribute("placeholder")).toContain("已配置，留空不覆盖");
  });
  it("filters the channel matrix by missing configuration", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/channels" />);

    const table = await screen.findByRole("table", { name: "通道支持矩阵列表" });
    await user.type(screen.getByRole("textbox", { name: "按缺失配置筛选" }), "WECOM_BOT_WEBHOOK_KEY");

    expect(within(table).getByText("企微智能机器人")).not.toBeNull();
    expect(within(table).queryByText("飞书")).toBeNull();
  });

  it("clears saved Feishu settings from the visible form state", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/channels" />);

    await screen.findByRole("heading", { name: "通道连接" });
    expect(screen.getByRole("textbox", { name: /App ID FEISHU_APP_ID/ }).getAttribute("placeholder")).toContain("已配置");

    await user.click(screen.getByRole("button", { name: "清空当前通道配置" }));

    expect(await screen.findByText("通道配置已清空。需要重新填写后才会接通。")).not.toBeNull();
    expect(screen.getByText("还缺少配置：FEISHU_APP_ID, FEISHU_APP_SECRET")).not.toBeNull();
    expect(screen.getByRole("textbox", { name: /App ID FEISHU_APP_ID/ }).getAttribute("placeholder")).toBe("cli_xxx");
    expect(screen.queryByText("当前来源：本页保存")).toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/channels/feishu/config" && request.method === "DELETE")).toBeTruthy();
  });
  it("lets operators type and save channel configuration values", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/channels" />);

    await user.click(await screen.findByRole("button", { name: /钉钉/ }));
    await user.type(screen.getByLabelText(/App Key/), "ding-app-key");
    await user.type(screen.getByLabelText(/App Secret/), "ding-secret");
    await user.type(screen.getByLabelText(/Webhook Token/), "ding-token");
    const actions = screen.getByRole("group", { name: "通道配置操作" });
    expect(within(actions).getByRole("button", { name: "保存通道配置" })).not.toBeNull();
    expect(within(actions).getByRole("button", { name: "清空当前通道配置" })).not.toBeNull();
    await user.click(within(actions).getByRole("button", { name: "保存通道配置" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/channels/dingtalk/config")).toMatchObject({
        method: "POST",
        body: {
          values: {
            DINGTALK_APP_KEY: "ding-app-key",
            DINGTALK_APP_SECRET: "ding-secret",
            DINGTALK_WEBHOOK_TOKEN: "ding-token",
          },
        },
      }),
    );
    expect(await screen.findByText("通道配置已保存，可继续修改或清空。面板已刷新最新状态。")).not.toBeNull();
  });
});
