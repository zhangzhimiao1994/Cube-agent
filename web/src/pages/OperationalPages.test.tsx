import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelStatus, EvolutionRun, RunDetail, RunListItem } from "../api/client";
import { TestApp } from "../app/router";

const runId = "22222222-2222-4222-8222-222222222222";
const secondRunId = "33333333-3333-4333-8333-333333333333";
const conversationCreatedAt = "2026-08-07T00:00:00Z";
const conversationTimestamp = new Date(conversationCreatedAt)
  .toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })
  .replace(/\//g, "-");
const conversationHistoryTitle = `给我做一个短视频脚本方案。 · ${conversationTimestamp}`;
const escapedConversationHistoryTitle = conversationHistoryTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const conversationOpenButtonName = new RegExp(`进入会话 ${escapedConversationHistoryTitle}`);
const conversationBranchButtonName = new RegExp(`按原思路新建分支 ${escapedConversationHistoryTitle}`);
const conversationDeleteButtonName = new RegExp(`删除会话 ${escapedConversationHistoryTitle}`);
const runListItem: RunListItem = {
  id: runId,
  status: "running",
  mode: "dispatch",
  conversation_id: "conv-previous",
  request: "给我做一个短视频脚本方案。",
  created_at: conversationCreatedAt,
  queue_wait_ms: 120,
  capacity_wait_ms: 40,
  cost_usd: "0.0132",
};

const runDetail: RunDetail = {
  ...runListItem,
  request: "给我做一个短视频脚本方案。",
  events: [
    {
      sequence: 1,
      kind: "queued",
      message: "Run accepted and queued.",
      created_at: conversationCreatedAt,
      participants: [],
      payload: {},
    },
    {
      sequence: 2,
      kind: "model.started",
      message: "model.started",
      created_at: "2026-08-07T00:00:00.500Z",
      actor: "copywriter",
      participants: [],
      tool_name: null,
      step_id: null,
      action: null,
      decision: null,
      payload: {
        model: "qwen-max",
      },
    },
    {
      sequence: 3,
      kind: "artifact.created",
      message: "artifact.created",
      created_at: "2026-08-07T00:00:01Z",
      actor: "copywriter",
      participants: [],
      tool_name: "artifact_writer",
      step_id: "write-script",
      action: null,
      decision: null,
      payload: {
        summary: "写入短视频脚本",
        result: "得到一版可拍摄脚本文案",
        api_key: "[redacted]",
      },
    },
    {
      sequence: 4,
      kind: "discussion.completed",
      message: "导演、文案和剪辑师完成讨论，主 Agent 采用可拍摄性最高的方案。",
      created_at: "2026-08-07T00:00:02Z",
      actor: "director",
      participants: ["director", "copywriter", "editor"],
      tool_name: null,
      step_id: null,
      action: null,
      decision: "adopt",
      payload: {
        director_opinion: "导演认为要优先可拍摄性。",
        copywriter_opinion: "文案建议强化开头钩子。",
        editor_opinion: "剪辑师建议三段式节奏。",
        main_agent_judgement: "主 Agent 选择可拍摄性最高且风险最低的方案。",
        result: "采用可拍摄性最高的方案",
      },
    },
  ],
  artifacts: [
    {
      id: "artifact-1",
      kind: "markdown",
      title: "短视频脚本",
      text: "这是最终回复正文：导演、文案和剪辑师已经汇总出一个短视频脚本方案。",
    },
  ],
  explicit_details: {
    workflow_id: "short-video-dispatch",
    workflow_adjustment_policy: "ask_before_apply",
    selected_agent_ids: "director,copywriter,editor",
    routing_reason: "workflow selected explicitly",
    conversation_id: "conv-previous",
  },
};

const settings = {
  default_mode: "auto",
  default_workflow_id: null,
  default_agent_ids: [],
  log_level: "warning",
  hermes_enabled: true,
  safe_tools_enabled: true,
  require_approval_for_tools: true,
  allow_main_agent_override: true,
  allow_temporary_agents: true,
  vibe_coding_enabled: true,
  temporary_agent_policy: "全局策略：缺少专业能力时先询问用户，再临时加入子 Agent。",
  channel_entry: "web",
  attachment_retention_days: 7,
  attachment_max_mb: 25,
};

const mainAgent = {
  model: null,
  control_mode: "supervisor",
  decision_policy: "按证据、风险和产物质量裁决。",
  operating_style: "控场优先，直连时选择明确的模型/API回答。",
  direct_answerer: "",
  hermes_policy: "confirm_before_apply",
  max_review_rounds: 2,
};
const baseChannels: ChannelStatus[] = [
  {
    id: "feishu",
    name: "飞书",
    status: "missing_config",
    transports: ["webhook", "websocket"],
    webhook_path: "/channels/feishu/events",
    public_webhook_url: null,
    missing: ["FEISHU_APP_ID"],
    configured: ["FEISHU_TRANSPORT"],
    configured_sources: { FEISHU_TRANSPORT: "environment" },
    command_aliases: {},
    notes: ["Webhook 已挂载在主 API 服务。"],
  },
  {
    id: "custom_webhook",
    name: "自定义 Webhook",
    status: "missing_config",
    transports: ["webhook"],
    webhook_path: "/channels/custom/events",
    public_webhook_url: null,
    missing: ["CUSTOM_WEBHOOK_TOKEN"],
    configured: [],
    configured_sources: {},
    command_aliases: {},
    notes: ["用于兼容其他支持 HTTP Webhook 的聊天软件。"],
  },
];

const secondRunListItem: RunListItem = {
  ...runListItem,
  id: secondRunId,
  status: "completed",
  mode: "direct",
};

const hermesInsight = {
  id: "hermes_run_11111111111111111111111111111111",
  outcome: "success",
  category: "conversation",
  lesson: "Use group chat when debate review is required.",
  summary: "Learned success pattern: Use group chat when debate review is required. Tags: debate, review. Weight: 5.",
  run_id: runId,
  conversation_id: "conv-architecture-1",
  confirmed_at: null,
  tags: ["debate", "review"],
  weight: 5,
  created_at: "2026-08-07T00:04:00Z",
};

const secondHermesInsight = {
  ...hermesInsight,
  id: "hermes_run_22222222222222222222222222222222",
  outcome: "failure",
  category: "scheduler",
  lesson: "Ask for confirmation before changing the workflow role pool.",
  summary: "Learned failure pattern: Ask for confirmation before changing the workflow role pool. Tags: workflow, approval. Weight: 4.",
  conversation_id: "conv-workflow-2",
  confirmed_at: null,
  tags: ["workflow", "approval"],
  weight: 4,
  created_at: "2026-08-07T00:06:00Z",
};

const agents = [
  {
    id: "director",
    name: "导演",
    enabled: true,
    role: "导演",
    prompt: "负责选题、分镜和最终把关。",
    model: "main",
    skills: [],
  },
  {
    id: "copywriter",
    name: "文案生成",
    enabled: true,
    role: "文案生成",
    prompt: "负责脚本与口播。",
    model: "main",
    skills: [],
  },
  {
    id: "editor",
    name: "剪辑师",
    enabled: true,
    role: "剪辑师",
    prompt: "负责镜头节奏和剪辑建议。",
    model: "main",
    skills: [],
  },
  {
    id: "analyst-unbound",
    name: "未绑定模型分析师",
    enabled: true,
    role: "经济分析师",
    prompt: "用于验证直连前必须检查模型/API。",
    model: "missing-model",
    skills: [],
  },
];

const models = [
  {
    id: "model-main",
    provider: "deepseek",
    api_base: "https://api.deepseek.com/v1",
    api_protocol: "openai_compatible",
    upstream_model: "deepseek-v4-flash",
    logical_model: "main",
    capabilities: ["chat"],
    credential_ref: "secret://main",
    quota_scope: "deepseek",
    max_concurrency: 4,
    target_utilization: 0.7,
    reserved_capacity: 0,
    rpm: 60,
    tpm: null,
    queue_timeout_seconds: 30,
    fallback: null,
    weight: 1,
    effective_slots: 3,
    saturation_policy: "queue",
  },
  {
    id: "model-coder",
    provider: "qwen",
    api_base: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_protocol: "openai_compatible",
    upstream_model: "qwen-max",
    logical_model: "coder",
    capabilities: ["chat", "code"],
    credential_ref: "secret://coder",
    quota_scope: "qwen",
    max_concurrency: 2,
    target_utilization: 0.7,
    reserved_capacity: 0,
    rpm: 60,
    tpm: null,
    queue_timeout_seconds: 30,
    fallback: null,
    weight: 1,
    effective_slots: 1,
    saturation_policy: "queue",
  },
];


const evolutionRun: EvolutionRun = {
  id: "evolution_11111111111111111111111111111111",
  kind: "skill_optimization",
  title: "Darwin Skill 迭代",
  objective: "用固定评测集优化 darwin-skill，未达标不发布。",
  mode: "hybrid",
  source_skill_ids: ["darwin-skill"],
  source_conversation_id: "conv-evolution-darwin",
  source_run_id: null,
  target_artifact_type: "skill",
  baseline_agent_id: "agent-main-m3",
  candidate_agent_ids: ["agent-coder", "agent-reviewer"],
  evaluator_agent_id: "agent-evaluator",
  approval_policy: "ask",
  approval_status: "approved",
  approved_by: "11111111-1111-4111-8111-111111111111",
  approved_at: "2026-08-14T09:59:00Z",
  approval_note: "人工确认基准 agent。",
  iteration_policy: "score_gated",
  memory_policy: "summarize_between_rounds",
  next_action: "run_next_round",
  status: "running",
  max_rounds: 5,
  min_delta: 2,
  budget_tokens: 200000,
  budget_minutes: 120,
  rubric: ["实测表现", "反例覆盖"],
  rounds: [
    {
      round: 1,
      changed_dimension: "实测表现",
      candidate_summary: "补充测试 prompt 并降低自评偏差。",
      score_before: 72,
      score_after: 76.5,
      delta: 4.5,
      tests_passed: true,
      regression_detected: false,
      accepted: true,
      recommendation: "continue",
      stop_reason: null,
      judge_summary: "两个测试 prompt 均优于基线。",
      artifact_refs: ["artifact://generated-skill/darwin-v2"],
      tokens_used: 12000,
      elapsed_seconds: 180,
      created_at: "2026-08-14T10:00:00Z",
    },
  ],
  created_by: "11111111-1111-4111-8111-111111111111",
  created_at: "2026-08-14T09:00:00Z",
  updated_at: "2026-08-14T10:00:00Z",
  stop_reason: null,
};
const workflows = [
  {
    id: "short-video-dispatch",
    name: "短视频派单",
    enabled: true,
    mode: "dispatch",
    allow_main_agent_override: false,
    allow_temporary_agents: false,
    temporary_agent_policy: "旧工作流内策略应被全局设置取代。",
    task_type: "短视频内容生产",
    role_selection_policy: "导演、文案、剪辑师参与；不默认派给程序员。",
    agent_ids: ["director", "copywriter", "editor"],
    objective: "产出短视频脚本方案",
    steps: ["拆解需求", "角色分工", "汇总产物"],
    deliverables: ["脚本", "分镜", "剪辑建议"],
    decision_policy: "主 Agent 汇总裁决",
  },
];

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("operational management pages", () => {
  const requests: Array<{ body: unknown; headers: Record<string, string>; method: string; path: string }> = [];
  let visibleRunListItem = runListItem;
  let visibleRunDetail = runDetail;
  let visibleConversationRuns = [runDetail];
  let visibleRunListItems = [runListItem];
  let visibleModels = models;
  let visibleWorkflows = workflows;
  let deletedRunIds = new Set<string>();
  let deletedHermesIds = new Set<string>();
  let visibleEvolutionRuns = [evolutionRun];
  let visibleChannels = baseChannels;
  let createdEvolutionRun: typeof evolutionRun | null = null;
  let failNextAttachmentUpload = false;
  let holdActiveConversationRequest = false;

  beforeEach(() => {
    requests.length = 0;
    visibleRunListItem = runListItem;
    visibleRunDetail = runDetail;
    visibleConversationRuns = [visibleRunDetail];
    visibleRunListItems = [visibleRunListItem];
    visibleModels = models;
    visibleWorkflows = workflows;
    deletedRunIds = new Set<string>();
    deletedHermesIds = new Set<string>();
    visibleEvolutionRuns = [evolutionRun];
    visibleChannels = baseChannels;
    createdEvolutionRun = null;
    failNextAttachmentUpload = false;
    holdActiveConversationRequest = false;
    vi.stubGlobal("confirm", vi.fn(() => true));
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        const requestHeaders = Object.fromEntries(new Headers(init?.headers).entries());
        if (init?.body && typeof init.body === "string") {
          requests.push({ path, method, headers: requestHeaders, body: JSON.parse(init.body) });
        } else {
          requests.push({ path, method, headers: requestHeaders, body: null });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse(visibleRunListItems.filter((item) => !deletedRunIds.has(item.id)));
        }
        if (path === `/api/v1/admin/runs/${runId}` && method === "DELETE") {
          deletedRunIds.add(runId);
          return jsonResponse({ id: runId, deleted: true });
        }
        if (path === `/api/v1/admin/runs/${secondRunId}` && method === "DELETE") {
          deletedRunIds.add(secondRunId);
          return jsonResponse({ id: secondRunId, deleted: true });
        }
        if (path === "/api/v1/admin/runs/bulk-delete" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : { ids: [] };
          const ids = Array.isArray(body.ids) ? body.ids : [];
          ids.forEach((id: unknown) => deletedRunIds.add(String(id)));
          return jsonResponse({
            deleted: ids.map((id: unknown) => ({ id, deleted: true })),
            failed: [],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) {
          if (deletedRunIds.has(runId)) {
            return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
          }
          return jsonResponse(visibleRunDetail);
        }
        if (path === "/api/v1/admin/conversations/conv-previous") {
          return jsonResponse({ conversation_id: "conv-previous", runs: visibleConversationRuns });
        }
        if (path === `/api/v1/admin/conversations/${runDetail.explicit_details.conversation_id}`) {
          if (holdActiveConversationRequest) {
            return new Promise<Response>(() => undefined);
          }
          return jsonResponse({ conversation_id: runDetail.explicit_details.conversation_id, runs: visibleConversationRuns });
        }
        if (path === `/api/v1/admin/runs/${runId}/pause`) {
          return jsonResponse({ ...runDetail, status: "paused" });
        }
        if (path === `/api/v1/admin/runs/${runId}/cancel`) {
          visibleRunListItems = visibleRunListItems.map((item) =>
            item.id === runId ? { ...item, status: "cancelled" } : item,
          );
          visibleRunDetail = { ...visibleRunDetail, status: "cancelled" };
          visibleConversationRuns = visibleConversationRuns.map((item) =>
            item.id === runId ? { ...item, status: "cancelled" } : item,
          );
          return jsonResponse(visibleRunDetail);
        }
        if (path === "/api/v1/runs" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          const message = String(body.message ?? "");
          if (message.includes("二次确认")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_user_mode",
              mode: null,
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "routing_requires_user_choice",
            });
          }
          if (message.includes("进化 darwin-skill")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_approval",
              mode: "hybrid",
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "evolution_requires_user_confirmation",
              conversation_id: typeof body.conversation_id === "string" ? body.conversation_id : null,
              evolution_proposal: {
                kind: "skill_optimization",
                title: "Skill 进化任务",
                objective: message,
                mode: "hybrid",
                source_skill_ids: ["darwin-skill"],
                source_conversation_id: typeof body.conversation_id === "string" ? body.conversation_id : null,
                source_run_id: null,
                target_artifact_type: "skill",
                baseline_agent_id: "main-agent",
                candidate_agent_ids: ["worker-agent", "reviewer-agent"],
                evaluator_agent_id: "evaluator-agent",
                approval_policy: "ask",
                iteration_policy: "score_gated",
                memory_policy: "summarize_between_rounds",
                max_rounds: 5,
                min_delta: 2,
                budget_tokens: 200000,
                budget_minutes: 120,
                rubric: ["实测表现", "反例覆盖", "人工验收"],
                summary: "主 Agent 判断这条消息适合进入进化任务。",
                metadata: {
                  source: "chat_evolution_proposal",
                  requires_user_confirmation: "true",
                },
              },
            });
          }
          if (message.includes("OpenClaw")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_approval",
              mode: "dispatch",
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "openclaw_requires_user_confirmation",
              conversation_id: typeof body.conversation_id === "string" ? body.conversation_id : null,
              openclaw_proposal: {
                kind: "server_command",
                platform: "linux",
                target_type: "server",
                target: "linux-server",
                operation_text: message,
                source_conversation_id: typeof body.conversation_id === "string" ? body.conversation_id : null,
                summary: "主 Agent 检测到 OpenClaw 服务器操作请求。",
                metadata: {
                  source: "chat_openclaw_proposal",
                  requires_user_confirmation: "true",
                },
              },
            });
          }
          if (message.includes("每天9点提醒")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_approval",
              mode: "dispatch",
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "schedule_requires_user_confirmation",
              schedule_proposal: {
                name: "chat-daily-schedule",
                message,
                mode: "dispatch",
                workflow_id: "scheduled_task",
                kind: "cron",
                timezone: "Asia/Shanghai",
                misfire_policy: "fire_once",
                budget: 16384,
                run_at: null,
                cron: "0 9 * * *",
                summary: "每天 09:00 执行。",
                metadata: {
                  source: "chat_schedule_proposal",
                  requires_user_confirmation: "true",
                },
              },
            });
          }
          if (message.includes("网页") || message.toLowerCase().includes("web page")) {
            return jsonResponse({
              id: runId,
              tenant_id: "33333333-3333-4333-8333-333333333333",
              status: "waiting_approval",
              mode: "dispatch",
              decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
              version: 1,
              clarification_reason: "temporary_agent_requires_user_approval",
              temporary_agent_proposal: {
                id: "temp-web-engineer",
                name: "Temporary Web Engineer",
                role: "Web Engineer",
                prompt: "把方案落成网页并说明验证步骤。",
                reason: "当前角色池缺少 software_engineering 能力。",
                missing_capability: "software_engineering",
                suggested_skills: ["frontend"],
                permanentizable: true,
              },
            });
          }
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "dispatch",
            decision_token: null,
            version: 1,
            clarification_reason: null,
            conversation_id: typeof body.conversation_id === "string" ? body.conversation_id : null,
            reference_conversation_id:
              typeof body.reference_conversation_id === "string" ? body.reference_conversation_id : null,
          });
        }
        if (path === `/api/v1/runs/${runId}/choose-mode` && method === "POST") {
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "discuss",
            decision_token: null,
            version: 2,
            clarification_reason: null,
          });
        }
        if (path === `/api/v1/runs/${runId}/approve-temporary-agent` && method === "POST") {
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "dispatch",
            decision_token: null,
            version: 2,
            clarification_reason: null,
          });
        }
        if (path === `/api/v1/runs/${runId}/revise-temporary-agent` && method === "POST") {
          return jsonResponse({
            id: runId,
            tenant_id: "33333333-3333-4333-8333-333333333333",
            status: "queued",
            mode: "dispatch",
            decision_token: null,
            version: 2,
            clarification_reason: null,
          });
        }
        if (path === "/api/v1/admin/settings") {
          return jsonResponse(settings);
        }
        if (path === "/api/v1/admin/main-agent") {
          return jsonResponse(mainAgent);
        }
        if (path === `/api/v1/admin/openclaw/operations/from-run/${runId}` && method === "POST") {
          return jsonResponse(
            {
              id: "openclaw_from_chat_1",
              status: "waiting_user_approval",
              approval_id: "openclaw_from_chat_1_approval",
              requires_user_approval: true,
              platform: "linux",
              kind: "server_command",
              operation: {
                platform: "linux",
                kind: "server_command",
                target: "linux-server",
                argv: ["date"],
                risk_level: "medium",
                reason: "Created from chat proposal.",
              },
              approval_summary: "OpenClaw linux server command from chat proposal.",
              requested_by: "11111111-1111-4111-8111-111111111111",
              created_at: "2026-08-15T02:00:00Z",
              resolved_by: null,
              resolved_at: null,
              execution: null,
            },
            { status: 202 },
          );
        }
        if (path === "/api/v1/admin/schedules" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          return jsonResponse({
            id: "44444444-4444-4444-8444-444444444444",
            status: "active",
            next_fire_at: "2026-08-15T01:00:00Z",
            ...body,
          });
        }
        if (path === "/api/v1/admin/agents" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          return jsonResponse(body);
        }
        if (path === "/api/v1/admin/agents") {
          return jsonResponse(agents);
        }
        if (path === "/api/v1/admin/models") {
          return jsonResponse(visibleModels);
        }
        if (path === "/api/v1/admin/skills/upload" && method === "POST") {
          return jsonResponse({
            filename: "uploaded-skill.zip",
            bundle: false,
            items: [
              {
                id: "skill-uploaded-from-chat",
                name: "uploaded_skill",
                version: "1.0.0",
                status: "scanned",
                requested_permissions: ["tool:filesystem.read"],
                scan_diff: ["content sha256: abc123", "entry point: main.py"],
              },
            ],
          });
        }
        if (path === "/api/v1/admin/skills/skill-uploaded-from-chat/approve" && method === "POST") {
          return jsonResponse({
            id: "skill-uploaded-from-chat",
            name: "uploaded_skill",
            version: "1.0.0",
            status: "enabled",
            requested_permissions: ["tool:filesystem.read"],
            scan_diff: ["content sha256: abc123", "entry point: main.py", "approved by production admin"],
          });
        }
        if (path === "/api/v1/admin/evolution-runs" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          createdEvolutionRun = {
            ...evolutionRun,
            id: "evolution_22222222222222222222222222222222",
            title: String(body.title ?? "新进化任务"),
            objective: String(body.objective ?? ""),
            source_skill_ids: Array.isArray(body.source_skill_ids) ? body.source_skill_ids : [],
            baseline_agent_id: typeof body.baseline_agent_id === "string" ? body.baseline_agent_id : "",
            candidate_agent_ids: Array.isArray(body.candidate_agent_ids) ? body.candidate_agent_ids : [],
            evaluator_agent_id: typeof body.evaluator_agent_id === "string" ? body.evaluator_agent_id : "",
            approval_policy: typeof body.approval_policy === "string" ? body.approval_policy : "ask",
            approval_status: "pending",
            approved_by: "",
            approved_at: "",
            approval_note: "",
            iteration_policy: typeof body.iteration_policy === "string" ? body.iteration_policy : "score_gated",
            memory_policy: typeof body.memory_policy === "string" ? body.memory_policy : "summarize_between_rounds",
            next_action: "request_approval",
            rounds: [],
            status: "waiting_approval",
          };
          visibleEvolutionRuns = [createdEvolutionRun, ...visibleEvolutionRuns];
          return jsonResponse(createdEvolutionRun);
        }
        const approveEvolutionMatch = path.match(/^\/api\/v1\/admin\/evolution-runs\/(evolution_[a-f0-9]+)\/approve$/);
        if (approveEvolutionMatch && method === "POST") {
          const id = approveEvolutionMatch[1];
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          const current = visibleEvolutionRuns.find((run) => run.id === id) ?? evolutionRun;
          const approved = {
            ...current,
            status: body.approved === false ? "stopped" : "running",
            approval_status: body.approved === false ? "rejected" : "approved",
            approved_by: "11111111-1111-4111-8111-111111111111",
            approved_at: "2026-08-14T10:10:00Z",
            approval_note: String(body.note ?? "人工确认基准 agent。"),
            baseline_agent_id: typeof body.baseline_agent_id === "string" ? body.baseline_agent_id : current.baseline_agent_id,
            evaluator_agent_id: typeof body.evaluator_agent_id === "string" ? body.evaluator_agent_id : current.evaluator_agent_id,
            next_action: body.approved === false ? "stop" : "run_next_round",
          };
          visibleEvolutionRuns = visibleEvolutionRuns.map((run) => (run.id === id ? approved : run));
          if (createdEvolutionRun?.id === id) createdEvolutionRun = approved;
          return jsonResponse(approved);
        }
        const roundEvolutionMatch = path.match(/^\/api\/v1\/admin\/evolution-runs\/(evolution_[a-f0-9]+)\/rounds$/);
        if (roundEvolutionMatch && method === "POST") {
          const id = roundEvolutionMatch[1];
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : {};
          const current = visibleEvolutionRuns.find((run) => run.id === id) ?? evolutionRun;
          const round = {
            round: current.rounds.length + 1,
            changed_dimension: String(body.changed_dimension ?? ""),
            candidate_summary: String(body.candidate_summary ?? ""),
            score_before: Number(body.score_before ?? 0),
            score_after: Number(body.score_after ?? 0),
            delta: Number(body.score_after ?? 0) - Number(body.score_before ?? 0),
            tests_passed: Boolean(body.tests_passed),
            regression_detected: Boolean(body.regression_detected),
            accepted: body.accepted === true,
            recommendation: body.regression_detected ? "rollback" : "continue",
            stop_reason: body.regression_detected ? "tests regressed or score did not improve" : null,
            judge_summary: String(body.judge_summary ?? ""),
            artifact_refs: Array.isArray(body.artifact_refs) ? body.artifact_refs : [],
            tokens_used: Number(body.tokens_used ?? 0),
            elapsed_seconds: Number(body.elapsed_seconds ?? 0),
            created_at: "2026-08-14T10:12:00Z",
          };
          const updated = {
            ...current,
            rounds: [...current.rounds, round],
            status: body.regression_detected ? "stopped" : "running",
            next_action: body.regression_detected ? "rollback_candidate" : "run_next_round",
            stop_reason: body.regression_detected ? "tests regressed or score did not improve" : current.stop_reason,
          };
          visibleEvolutionRuns = visibleEvolutionRuns.map((run) => (run.id === id ? updated : run));
          if (createdEvolutionRun?.id === id) createdEvolutionRun = updated;
          return jsonResponse(updated);
        }
        const nextRoundPlanMatch = path.match(/^\/api\/v1\/admin\/evolution-runs\/(evolution_[a-f0-9]+)\/next-round-plan$/);
        if (nextRoundPlanMatch && method === "GET") {
          const id = nextRoundPlanMatch[1];
          const current = visibleEvolutionRuns.find((run) => run.id === id) ?? evolutionRun;
          return jsonResponse({
            run_id: id,
            round: current.rounds.length + 1,
            action: "run_next_round",
            task_title: `${current.title} / round ${current.rounds.length + 1}`,
            task_prompt: [
              `Evolution run: ${current.title}`,
              `Objective: ${current.objective}`,
              `Source skills: ${current.source_skill_ids.join(", ") || "none"}`,
              "固定评测集比较基准和候选，输出 score_before 和 score_after。",
            ].join("\n"),
            baseline_agent_id: current.baseline_agent_id,
            candidate_agent_ids: current.candidate_agent_ids,
            evaluator_agent_id: current.evaluator_agent_id,
            memory_policy: current.memory_policy,
            required_output_schema: {
              score_before: "Baseline score before candidate changes.",
              score_after: "Candidate score after changes.",
            },
            previous_rounds: current.rounds.map((round) => `round ${round.round}: ${round.recommendation}`),
          });
        }
        const executeEvolutionMatch = path.match(/^\/api\/v1\/admin\/evolution-runs\/(evolution_[a-f0-9]+)\/execute-next-round$/);
        if (executeEvolutionMatch && method === "POST") {
          const id = executeEvolutionMatch[1];
          const current = visibleEvolutionRuns.find((run) => run.id === id) ?? evolutionRun;
          return jsonResponse({
            evolution_run_id: id,
            round: current.rounds.length + 1,
            action: "run_next_round",
            execution_run_id: "44444444-4444-4444-8444-444444444444",
            execution_conversation_id: `${id}-round-${current.rounds.length + 1}`,
            status: "queued",
            task_title: `${current.title} / round ${current.rounds.length + 1}`,
            task_prompt: "Execute one bounded evolution round.",
          });
        }
        const ingestEvolutionMatch = path.match(/^\/api\/v1\/admin\/evolution-runs\/(evolution_[a-f0-9]+)\/execution-runs\/([0-9a-f-]+)\/ingest$/);
        if (ingestEvolutionMatch && method === "POST") {
          const id = ingestEvolutionMatch[1];
          const executionRunId = ingestEvolutionMatch[2];
          const current = visibleEvolutionRuns.find((run) => run.id === id) ?? evolutionRun;
          const round = {
            round: current.rounds.length + 1,
            changed_dimension: "执行结果导入",
            candidate_summary: "执行运行产物已导入。",
            score_before: 76,
            score_after: 81,
            delta: 5,
            tests_passed: true,
            regression_detected: false,
            accepted: true,
            recommendation: "accept_candidate",
            stop_reason: null,
            judge_summary: "从执行运行产物自动导入。",
            artifact_refs: [`run://${executionRunId}`],
            tokens_used: 2048,
            elapsed_seconds: 120,
            created_at: "2026-08-14T10:13:00Z",
          };
          const updated = {
            ...current,
            rounds: [...current.rounds, round],
            status: "running",
            next_action: "run_next_round",
          };
          visibleEvolutionRuns = visibleEvolutionRuns.map((run) => (run.id === id ? updated : run));
          if (createdEvolutionRun?.id === id) createdEvolutionRun = updated;
          return jsonResponse(updated);
        }
        if (path === "/api/v1/admin/evolution-runs") {
          return jsonResponse(visibleEvolutionRuns);
        }
        if (path === "/api/v1/admin/channels") {
          return jsonResponse(visibleChannels);
        }
        if (path === "/api/v1/admin/channels/custom_webhook/config" && method === "POST") {
          visibleChannels = visibleChannels.map((channel) =>
            channel.id === "custom_webhook" ? { ...channel, status: "configured", missing: [] } : channel,
          );
          return jsonResponse({ id: "custom_webhook", saved: ["CUSTOM_WEBHOOK_TOKEN"], status: visibleChannels.find((channel) => channel.id === "custom_webhook") });
        }
        if (path === "/api/v1/admin/channels/custom_webhook/config" && method === "DELETE") {
          visibleChannels = visibleChannels.map((channel) =>
            channel.id === "custom_webhook" ? { ...channel, status: "missing_config", missing: ["CUSTOM_WEBHOOK_TOKEN"] } : channel,
          );
          return jsonResponse({ id: "custom_webhook", saved: [], status: visibleChannels.find((channel) => channel.id === "custom_webhook") });
        }
        if (path === "/api/v1/admin/skills") {
          return jsonResponse([]);
        }
        if (path === "/api/v1/runs/attachments/upload" && method === "POST") {
          if (failNextAttachmentUpload) {
            failNextAttachmentUpload = false;
            throw new TypeError("Failed to fetch");
          }
          const headers = init?.headers instanceof Headers ? init.headers : new Headers(init?.headers);
          const rawFilename = headers.get("X-Agent-Hub-Filename") ?? "screen.png";
          const filename = headers.get("X-Agent-Hub-Filename-Encoding") === "percent" ? decodeURIComponent(rawFilename) : rawFilename;
          const contentType = headers.get("Content-Type") ?? "image/png";
          const archive = /\.(?:zip|tar|tgz|gz|bz2|xz|zst|rar|7z|cab|iso|jar|war|ear|apk|ipa)$/i.test(filename);
          return jsonResponse({
            id: "att_0123456789abcdef0123456789abcdef",
            filename,
            kind: archive ? "archive" : contentType.startsWith("image/") ? "image" : "context",
            content_type: contentType,
            size_bytes: 128,
            sha256: "a".repeat(64),
            expires_at: "2026-08-17T00:00:00Z",
          });
        }
        if (path === "/api/v1/admin/workflows") {
          return jsonResponse(visibleWorkflows);
        }
        if (path === "/api/v1/admin/hermes") {
          return jsonResponse([hermesInsight, secondHermesInsight].filter((item) => !deletedHermesIds.has(item.id)));
        }
        if (path === "/api/v1/admin/hermes/hermes_run_11111111111111111111111111111111" && method === "DELETE") {
          deletedHermesIds.add("hermes_run_11111111111111111111111111111111");
          return jsonResponse({ status: "deleted" });
        }
        if (path === "/api/v1/admin/hermes/hermes_run_11111111111111111111111111111111") {
          return jsonResponse(hermesInsight);
        }
        if (path === "/api/v1/admin/hermes/hermes_run_11111111111111111111111111111111/confirm" && method === "POST") {
          return jsonResponse({ ...hermesInsight, confirmed_at: "2026-08-07T00:05:00Z" });
        }
        if (path === "/api/v1/admin/hermes/hermes_run_22222222222222222222222222222222") {
          return jsonResponse(secondHermesInsight);
        }
        if (path === "/api/v1/admin/hermes/hermes_run_22222222222222222222222222222222/confirm" && method === "POST") {
          return jsonResponse({ ...secondHermesInsight, confirmed_at: "2026-08-07T00:07:00Z" });
        }
        if (path === "/api/v1/admin/hermes/bulk-confirm" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : { ids: [] };
          const ids = Array.isArray(body.ids) ? body.ids : [];
          return jsonResponse({
            confirmed: ids.map((id: unknown) =>
              id === "hermes_run_22222222222222222222222222222222"
                ? { ...secondHermesInsight, confirmed_at: "2026-08-07T00:07:00Z" }
                : { ...hermesInsight, confirmed_at: "2026-08-07T00:05:00Z" },
            ),
            failed: [],
          });
        }
        if (path === "/api/v1/admin/hermes/bulk-delete" && method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : { ids: [] };
          const ids = Array.isArray(body.ids) ? body.ids : [];
          ids.forEach((id: unknown) => {
            if (typeof id === "string") deletedHermesIds.add(id);
          });
          return jsonResponse({ deleted: ids, failed: [] });
        }
        if (path === "/api/v1/admin/mcp") {
          return jsonResponse([{ id: "filesystem", name: "Filesystem MCP", health: "healthy", allowed_tools: ["read_file"] }]);
        }
        if (path === "/api/v1/admin/memory") {
          return jsonResponse([
            {
              id: "project-policy",
              scope: "tenant",
              value: "Only non-dangerous operations may run without approval.",
              heat: 0.82,
              locked: true,
              project_id: "cube-agent",
              conversation_id: "handoff",
              summary_period: "week",
              recall_count: 3,
              last_recalled_at: "2026-08-29T09:00:00Z",
            },
          ]);
        }
        if (path.startsWith("/api/v1/admin/logs")) {
          const logs = [
            {
              id: "model-error-1",
              category: "model_error",
              level: "error",
              title: "模型配置与调用错误",
              message: "provider returned status=401",
              source: "models.create",
              details: { provider: "deepseek", status_code: "401" },
              created_at: "2026-08-07T00:01:00Z",
            },
            {
              id: "model-warning-1",
              category: "model_error",
              level: "warning",
              title: "模型配置与调用警告",
              message: "anthropic preflight latency is high",
              source: "models.probe",
              details: { provider: "anthropic", status_code: "slow" },
              created_at: "2026-08-07T00:01:30Z",
            },
            {
              id: "mode-error-1",
              category: "mode_error",
              level: "error",
              title: "模式运行错误",
              message: "dispatch runtime failed",
              source: "runs.execute",
              details: { mode: "dispatch", run_id: runId },
              created_at: "2026-08-07T00:02:00Z",
            },
            {
              id: "audit-1",
              category: "audit",
              level: "info",
              title: "审计日志",
              message: "config.publish",
              source: "admin.audit",
              details: { resource: "configuration", actor: "system" },
              created_at: "2026-08-07T00:03:00Z",
            },
            {
              id: "audit-login-1",
              category: "audit",
              level: "info",
              title: "审计日志",
              message: "auth.login",
              source: "auth.login",
              details: { action: "auth.login", actor: "owner", user_id: "owner", ip: "127.0.0.1" },
              created_at: "2026-08-07T00:03:10Z",
            },
            {
              id: "audit-run-submit-1",
              category: "audit",
              level: "info",
              title: "审计日志",
              message: "run.submit",
              source: "admin.audit",
              details: {
                action: "run.submit",
                actor: "11111111-1111-4111-8111-111111111111",
                user_id: "11111111-1111-4111-8111-111111111111",
                user_role: "super_admin",
                resource: runId,
                run_id: runId,
                conversation_id: "conv-audit-user-1",
                reference_conversation_id: "conv-previous",
                mode: "auto",
                accepted_mode: "dispatch",
                status: "queued",
                message_preview: "请继续优化这个方案",
              },
              created_at: "2026-08-07T00:03:30Z",
            },
          ];
          const url = new URL(path, "https://agent-hub.test");
          const category = url.searchParams.get("category");
          return jsonResponse(category ? logs.filter((item) => item.category === category) : logs);
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    cleanup();
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  async function openRunConfig(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: /打开本次运行配置|open/i }));
  }

  it("shows run operations and supports pause control on the detail page", async () => {
    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    expect(screen.getByText("running")).not.toBeNull();
    expect(screen.getByText("markdown：短视频脚本")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "暂停" }));

    await waitFor(() => expect(screen.getByText("paused")).not.toBeNull());
  });

  it("shows observer notices as scheduler guidance on the detail page", async () => {
    visibleRunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 5,
          kind: "observer.notice",
          message: "observer.notice",
          created_at: "2026-08-07T00:00:03Z",
          actor: "reviewer",
          participants: [],
          tool_name: null,
          step_id: null,
          action: null,
          decision: null,
          payload: {
            trigger: "model_capacity_pressure",
            action: "reschedule_or_reassign_model",
            severity: "warning",
            source_kind: "step.failed",
            source_sequence: 4,
            failure_events: 1,
            retry_events: 0,
            message_events: 3,
            artifact_events: 1,
          },
        },
      ],
    };
    visibleConversationRuns = [visibleRunDetail];

    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "调度观察" })).not.toBeNull();
    expect(screen.getByText("模型容量拥堵")).not.toBeNull();
    expect(screen.getByText("建议改派模型或重新调度")).not.toBeNull();
    expect(screen.getByText(/来源：step\.failed #4/)).not.toBeNull();
    expect(screen.queryByText("null")).toBeNull();
  });
  it("stops the current running chat from the conversation composer", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(await screen.findByRole("button", { name: conversationOpenButtonName }));
    await user.click(await screen.findByRole("button", { name: "停止生成" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/admin/runs/${runId}/cancel`)).toMatchObject({
        method: "POST",
      }),
    );
    expect(await screen.findByText("已停止当前运行。你可以继续发送新消息。")).not.toBeNull();
  });
  it("keeps run detail access inside the center chat stream and sends selected workflow roles", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    expect(screen.getByText(/连续对话窗口/)).not.toBeNull();

    await openRunConfig(user);
    await user.selectOptions(screen.getByLabelText("使用工作流"), "short-video-dispatch");
    expect(screen.getByText(/全局临场策略已开启/)).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "给我做一个短视频脚本方案。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const link = await screen.findByRole("link", { name: "查看运行详情" });
    expect(link.getAttribute("href")).toBe(`/runs/${runId}`);
    expect(link.closest(".chat-stream")).not.toBeNull();
    expect(screen.getByText(/这轮回复使用/)).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "给我做一个短视频脚本方案。",
        mode: "dispatch",
        workflow_id: "short-video-dispatch",
        allow_workflow_adjustment: true,
        agent_ids: ["director", "copywriter", "editor"],
      },
    });
  });

  it("keeps live adjustment and temporary-agent switches out of workflow configuration", async () => {
    render(<TestApp initialPath="/workflows" />);

    expect(await screen.findByRole("heading", { name: "工作流配置" })).not.toBeNull();
    expect(screen.queryByText(/临场调整/)).toBeNull();
    expect(screen.queryByText(/临时子 Agent/)).toBeNull();
    expect(screen.queryByLabelText("临时 Agent 补位规则")).toBeNull();
  });

  it("filters and sorts saved workflows like an operational table", async () => {
    const user = userEvent.setup();
    visibleWorkflows = [
      ...workflows,
      {
        ...workflows[0],
        id: "research-hybrid",
        name: "学术研究混合流程",
        enabled: false,
        mode: "hybrid",
        task_type: "学术研究",
        agent_ids: ["researcher", "critic"],
        objective: "发现论文创新点并输出评审意见",
      },
    ];

    render(<TestApp initialPath="/workflows" />);

    expect(await screen.findByRole("table", { name: "已保存工作流列表" })).not.toBeNull();
    expect(screen.getByRole("searchbox", { name: "快速搜索工作流" })).not.toBeNull();
    expect(screen.getByLabelText("按工作流状态筛选")).not.toBeNull();
    expect(screen.getByLabelText("按工作流默认模式筛选")).not.toBeNull();
    expect(screen.getByText("显示 2 / 2")).not.toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "快速搜索工作流" }), "学术");
    expect(screen.getByText("学术研究混合流程")).not.toBeNull();
    expect(screen.queryByText("短视频派单")).toBeNull();

    await user.click(screen.getByRole("button", { name: "清空工作流筛选" }));
    expect(await screen.findByText("短视频派单")).not.toBeNull();

    await user.selectOptions(screen.getByLabelText("按工作流默认模式筛选"), "hybrid");
    expect(screen.getByText("学术研究混合流程")).not.toBeNull();
    expect(screen.queryByText("短视频派单")).toBeNull();

    await user.selectOptions(screen.getByLabelText("按工作流默认模式筛选"), "all");
    await user.selectOptions(screen.getByLabelText("按工作流状态筛选"), "enabled");
    expect(screen.getByText("短视频派单")).not.toBeNull();
    expect(screen.queryByText("学术研究混合流程")).toBeNull();

    await user.click(screen.getByRole("button", { name: "工作流排序" }));
    expect(screen.getByRole("button", { name: "工作流排序" }).textContent).toContain("↓");
  });
  it("loads an existing workflow into the form for editing", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/workflows" />);

    expect(await screen.findByRole("heading", { name: "工作流配置" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "编辑工作流" }));

    expect(screen.getByRole("status").textContent).toContain("已载入 短视频派单");
    expect((screen.getByLabelText("工作流 ID") as HTMLInputElement).value).toBe("short-video-dispatch");
    expect((screen.getByLabelText("执行步骤（每行一个）") as HTMLTextAreaElement).value).toBe(
      "拆解需求\n角色分工\n汇总产物",
    );

    await user.clear(screen.getByLabelText("工作流目标"));
    await user.type(screen.getByLabelText("工作流目标"), "更新后的短视频脚本方案");
    await user.click(screen.getByRole("button", { name: "保存工作流配置" }));

    expect(
      requests.find((request) => request.path === "/api/v1/admin/workflows" && request.method === "POST"),
    ).toMatchObject({
      body: {
        id: "short-video-dispatch",
        name: "短视频派单",
        objective: "更新后的短视频脚本方案",
        agent_ids: ["director", "copywriter", "editor"],
      },
    });
  });

  it("uses a selected direct model instead of a child agent when direct mode is selected", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    ["自动", "直连", "派单", "讨论", "混合"].forEach((label) => {
      expect(screen.getByRole("button", { name: label })).not.toBeNull();
    });
    expect(screen.queryByRole("button", { name: "选择直连模式" })).toBeNull();
    expect(screen.queryByRole("button", { name: "选择直连" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "直连" }));
    expect(await screen.findByText(/直连会由主 Agent 控场/)).not.toBeNull();
    expect(screen.getByText(/1\. main/)).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "1 帮我写一段口播。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "帮我写一段口播。",
        mode: "direct",
        allow_workflow_adjustment: false,
        direct_model: "main",
        agent_ids: [],
      },
    });
  });

  it("does not let direct mode silently fall back before a direct model is selected", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "直连" }));
    expect(screen.getByText(/直连需要先选择本次对话使用的模型\/API/)).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "直接回答这句话。");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(screen.getByText(/请先回复模型编号/)).not.toBeNull();
    expect(requests.filter((request) => request.path === "/api/v1/runs" && request.method === "POST")).toHaveLength(0);

    await user.clear(screen.getByPlaceholderText(/输入消息/));
    await user.type(screen.getByPlaceholderText(/输入消息/), "coder 直接回答这句话。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "直接回答这句话。",
        mode: "direct",
        direct_model: "coder",
        agent_ids: [],
      },
    });
  });

  it("shows an actionable empty state when direct mode has no configured models", async () => {
    const user = userEvent.setup();
    visibleRunListItems = [];
    visibleModels = [];
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "直连" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "请直接分析一下这个问题。");

    expect(screen.getAllByText(/还没有可用于直连的已测试模型/).length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
    expect(requests.filter((request) => request.path === "/api/v1/runs" && request.method === "POST")).toHaveLength(0);
  });

  it("renders text artifacts as assistant chat replies instead of artifact-only cards", async () => {
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);
    expect(within(stream).queryByText("产物：短视频脚本")).toBeNull();
  });

  it("renders markdown tables inside assistant chat replies as real tables", async () => {
    visibleRunDetail = {
      ...runDetail,
      artifacts: [
        {
          id: "artifact-table-reply",
          kind: "markdown",
          title: "回复",
          text: "对比如下：\n\n| 类型 | 能做 | 不能做 |\n| -- | -- | -- |\n| 个股 | 深度分析 | 主动推荐 |\n| 大类资产 | 趋势分析 | 实时下单 |\n\n请按这个边界使用。",
        },
      ],
    };
    visibleConversationRuns = [visibleRunDetail];
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    const table = within(stream).getByRole("table", { name: "回复表格 1" });
    expect(within(table).getByRole("columnheader", { name: "类型" })).not.toBeNull();
    expect(within(table).getByRole("cell", { name: "深度分析" })).not.toBeNull();
    expect(within(table).getByRole("cell", { name: "实时下单" })).not.toBeNull();
    expect(within(stream).getByText("请按这个边界使用。")).not.toBeNull();
  });
  it("does not show internal decision-review artifacts as the final assistant reply", async () => {
    visibleRunDetail = {
      ...runDetail,
      artifacts: [
        {
          id: "artifact-internal-review",
          kind: "markdown",
          title: "decision_recorder",
          text: "Result: 未满足用户目标。Evidence: 这是内部裁决记录，不应该直接展示成最终回复。",
        },
      ],
    };
    visibleConversationRuns = [visibleRunDetail];
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    const assistantReplies = within(stream).getAllByRole("article").filter((article) =>
      article.className.includes("assistant"),
    );
    expect(
      assistantReplies.some((article) => article.textContent?.includes("Result: 未满足用户目标")),
    ).toBe(false);
    expect(within(stream).getByText(/这轮只生成了内部审查或裁决内容/)).not.toBeNull();
  });

  it("does not keep the conversation loading skeleton when selected run detail is already visible", async () => {
    const user = userEvent.setup();
    holdActiveConversationRequest = true;
    visibleConversationRuns = [visibleRunDetail];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(await screen.findByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(await within(stream).findByText("给我做一个短视频脚本方案。")).not.toBeNull();
    expect(within(stream).queryByText("正在读取当前会话...")).toBeNull();
  });
  it("keeps older conversation messages when a later run is appended", async () => {
    visibleConversationRuns = [
      runDetail,
      {
        ...runDetail,
        id: secondRunId,
        request: "再给我一个更强的开头。",
        artifacts: [
          {
            id: "artifact-2",
            kind: "markdown",
            title: "短视频脚本二稿",
            text: "这是第二轮回复正文：已经把开头改得更强。",
          },
        ],
      },
    ];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText("给我做一个短视频脚本方案。")).not.toBeNull();
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);
    expect(await within(stream).findByText("再给我一个更强的开头。")).not.toBeNull();
    expect((await within(stream).findAllByText(/这是第二轮回复正文/)).length).toBeGreaterThan(0);
  });

  it("restores historical conversation messages after starting a new chat", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(await within(stream).findByText("给我做一个短视频脚本方案。")).not.toBeNull();
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);

    await user.click(screen.getAllByRole("button", { name: "新建对话" }).at(-1) as HTMLElement);
    expect(within(stream).queryByText("给我做一个短视频脚本方案。")).toBeNull();
    expect(screen.getByRole("button", { name: "自动" })).not.toBeNull();

    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    expect(await within(stream).findByText("给我做一个短视频脚本方案。")).not.toBeNull();
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);
    expect(screen.getByText(/会话：conv-previous/)).not.toBeNull();
  });

  it("opens a historical conversation and continues inside the same conversation id", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    await screen.findByText(/会话：conv-previous/);
    await user.type(screen.getByPlaceholderText(/输入消息/), "继续优化这个脚本。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.slice().reverse().find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "继续优化这个脚本。",
        conversation_id: "conv-previous",
      },
    });
  });

  it("sends a conversation-integrated Vibe Coding flag when enabled from the composer", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Vibe Coding" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "审查这个代码附件。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.slice().reverse().find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "审查这个代码附件。",
        vibe_coding: true,
      },
    });
  });

  it("creates a schedule from a chat-detected plan after user confirmation", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "每天9点提醒我填写日报");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "计划任务确认" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "加入计划" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/schedules" && request.method === "POST")).toMatchObject({
        body: {
          name: "chat-daily-schedule",
          message: "每天9点提醒我填写日报",
          mode: "dispatch",
          workflow_id: "scheduled_task",
          kind: "cron",
          cron: "0 9 * * *",
          timezone: "Asia/Shanghai",
        },
      }),
    );
    expect(await screen.findByText(/已加入计划/)).not.toBeNull();
  });
  it("can cancel a chat-detected schedule proposal before creating it", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "每天9点提醒我填写日报");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "计划任务确认" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "取消计划" }));

    await waitFor(() => expect(screen.queryByRole("status", { name: "计划任务确认" })).toBeNull());
    expect(requests.find((request) => request.path === "/api/v1/admin/schedules" && request.method === "POST")).toBeUndefined();
    expect(await screen.findByText("已取消计划任务创建，后续消息会继续作为普通对话处理。")).not.toBeNull();
  });
  it("creates an evolution run from a chat-detected evolution plan after user confirmation", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "请进化 darwin-skill，做多轮迭代");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "进化任务确认" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "加入进化" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/evolution-runs" && request.method === "POST")).toMatchObject({
        body: {
          kind: "skill_optimization",
          title: "Skill 进化任务",
          objective: "请进化 darwin-skill，做多轮迭代",
          mode: "hybrid",
          source_skill_ids: ["darwin-skill"],
          target_artifact_type: "skill",
          baseline_agent_id: "main-agent",
          evaluator_agent_id: "evaluator-agent",
          approval_policy: "ask",
          iteration_policy: "score_gated",
          memory_policy: "summarize_between_rounds",
        },
      }),
    );
    expect(await screen.findByText(/已加入进化/)).not.toBeNull();
  });
  it("can cancel a chat-detected evolution proposal before creating it", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "请进化 darwin-skill，做多轮迭代");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "进化任务确认" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "取消进化" }));

    await waitFor(() => expect(screen.queryByRole("status", { name: "进化任务确认" })).toBeNull());
    expect(requests.find((request) => request.path === "/api/v1/admin/evolution-runs" && request.method === "POST")).toBeUndefined();
    expect(await screen.findByText("已取消进化任务创建，后续消息会继续作为普通对话处理。")).not.toBeNull();
  });
  it("shows an OpenClaw operation proposal from chat and links to OpenClaw control", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "Use OpenClaw to execute date on the Linux server after approval.");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "OpenClaw 操作确认" })).not.toBeNull();
    expect(screen.getByText("linux-server")).not.toBeNull();
    expect(screen.getAllByText(/execute date/).length).toBeGreaterThan(0);

    expect(screen.getByRole("link", { name: "打开 OpenClaw" }).getAttribute("href")).toBe("/openclaw");
  });
  it("creates an OpenClaw operation from a chat proposal after user confirmation", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "Use OpenClaw to execute date on the Linux server after approval.");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("status", { name: "OpenClaw 操作确认" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "创建待审批操作" }));

    await waitFor(() =>
      expect(
        requests.find(
          (request) => request.path === `/api/v1/admin/openclaw/operations/from-run/${runId}` && request.method === "POST",
        ),
      ).toMatchObject({ body: null }),
    );
    expect(await screen.findByText(/已创建 OpenClaw 待审批操作/)).not.toBeNull();
    expect(screen.getByRole("link", { name: "打开 OpenClaw" }).getAttribute("href")).toBe("/openclaw");
  });
  it("clears failed attachment upload state and allows retrying the same file", async () => {
    failNextAttachmentUpload = true;
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    const input = screen.getByLabelText("上传文件或 Skill 压缩包") as HTMLInputElement;
    const file = new File(["image-bytes"], "截图.png", { type: "image/png" });

    await user.upload(input, file);
    expect(await screen.findByText(/附件上传失败: network request failed/)).not.toBeNull();
    expect(input.value).toBe("");

    await user.upload(input, file);
    expect(await screen.findByText("图片已上传。提交任务后会作为附件引用进入运行上下文。")).not.toBeNull();
    expect(screen.queryByText(/附件上传失败/)).toBeNull();

    const uploads = requests.filter((request) => request.path === "/api/v1/runs/attachments/upload");
    expect(uploads).toHaveLength(2);
    expect(uploads[1].headers["x-agent-hub-filename"]).toBe(encodeURIComponent("截图.png"));
  });
  it("separates composer tools, status, and send controls so actions do not crowd each other", async () => {
    const view = render(<TestApp initialPath="/" />);

    await waitFor(() => expect(view.container.querySelector(".chat-composer")).not.toBeNull());
    const composer = view.container.querySelector(".chat-composer") as HTMLFormElement;

    expect(composer.querySelector(".composer-tool-row")).not.toBeNull();
    expect(composer.querySelector(".composer-status-line")).not.toBeNull();
    expect(composer.querySelector(".composer-send-row")).not.toBeNull();
  });

  it("submits branch reference context and Vibe Coding together", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationBranchButtonName }));
    await screen.findByText(/已按原思路新建分支/);
    await user.click(screen.getByRole("button", { name: "Vibe Coding" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "沿用上一轮方向。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.slice().reverse().find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "沿用上一轮方向。",
        reference_conversation_id: "conv-previous",
        vibe_coding: true,
      },
    });
  });

  it("can cancel branch reference before sending a message", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationBranchButtonName }));
    await screen.findByText(/已按原思路新建分支/);
    expect(screen.queryByRole("button", { name: "按照原思路" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "取消引用会话" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "不引用上一轮。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const request = requests.slice().reverse().find((item) => item.path === "/api/v1/runs");
    expect(request).toMatchObject({
      method: "POST",
      body: {
        message: "不引用上一轮。",
        reference_conversation_id: null,
      },
    });
  });

  it("can cancel Vibe Coding mode before sending a message", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Vibe Coding" }));
    await user.click(screen.getByRole("button", { name: "Vibe Coding" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "正常对话。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.slice().reverse().find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "正常对话。",
        vibe_coding: false,
      },
    });
  });

  it("keeps branch reference and Vibe Coding as independent composer states", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationBranchButtonName }));
    await screen.findByText(/已按原思路新建分支/);

    const vibeButton = screen.getByRole("button", { name: "Vibe Coding" });

    await user.click(vibeButton);
    expect(screen.getByRole("button", { name: "取消引用会话" })).not.toBeNull();
    expect(vibeButton.getAttribute("aria-pressed")).toBe("true");
    expect(vibeButton.className).toContain("composer-vibe-active");

    await user.click(screen.getByRole("button", { name: "取消引用会话" }));
    expect(screen.queryByRole("button", { name: "取消引用会话" })).toBeNull();
    expect(vibeButton.getAttribute("aria-pressed")).toBe("true");

    await user.click(vibeButton);
    expect(vibeButton.getAttribute("aria-pressed")).toBe("false");
    expect(vibeButton.className).not.toContain("composer-vibe-active");
  });

  it("keeps multiple follow-up messages in the active conversation until the user starts a new chat", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    await screen.findByText(/会话：conv-previous/);

    await user.type(screen.getByPlaceholderText(/输入消息/), "继续优化这个脚本。");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => {
      const postRequests = requests.filter((request) => request.path === "/api/v1/runs");
      expect(postRequests.at(-1)).toMatchObject({
        body: { message: "继续优化这个脚本。", conversation_id: "conv-previous" },
      });
    });

    await user.type(screen.getByPlaceholderText(/输入消息/), "再给我一个更强的开头。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const postRequests = requests.filter((request) => request.path === "/api/v1/runs");
    expect(postRequests.slice(-2)).toMatchObject([
      { body: { message: "继续优化这个脚本。", conversation_id: "conv-previous" } },
      { body: { message: "再给我一个更强的开头。", conversation_id: "conv-previous" } },
    ]);
  });

  it("keeps cached conversation history when a fresh conversation fetch is temporarily incomplete", async () => {
    const user = userEvent.setup();
    const secondRunDetail = {
      ...runDetail,
      id: secondRunId,
      request: "再给我一个更强的开头。",
      artifacts: [
        {
          id: "artifact-2",
          kind: "markdown",
          title: "短视频脚本二稿",
          text: "这是第二轮回复正文：已经把开头改得更强。",
        },
      ],
    };
    visibleConversationRuns = [runDetail, secondRunDetail];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(await within(stream).findByText("给我做一个短视频脚本方案。")).not.toBeNull();
    expect((await within(stream).findAllByText(/这是第二轮回复正文/)).length).toBeGreaterThan(0);

    visibleConversationRuns = [secondRunDetail];
    await user.type(screen.getByPlaceholderText(/输入消息/), "继续。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await within(stream).findByText("给我做一个短视频脚本方案。")).not.toBeNull();
    expect((await within(stream).findAllByText(/这是第二轮回复正文/)).length).toBeGreaterThan(0);
  });

  it("starts a continuation branch when context is too long", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationBranchButtonName }));
    await screen.findByText(/已按原思路新建分支/);
    expect(screen.queryByRole("button", { name: "自动" })).toBeNull();
    expect(screen.queryByRole("button", { name: "直连" })).toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "接着前面的方向继续。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const request = requests.slice().reverse().find((item) => item.path === "/api/v1/runs");
    expect(request).toMatchObject({
      method: "POST",
      body: {
        message: "接着前面的方向继续。",
        reference_conversation_id: "conv-previous",
      },
    });
    expect((request?.body as { conversation_id?: string }).conversation_id).not.toBe("conv-previous");
  });

  it("keeps partial assistant output visible when a run fails after producing artifacts", async () => {
    visibleRunListItem = { ...runListItem, status: "failed", mode: "hybrid" };
    visibleRunListItems = [visibleRunListItem];
    visibleRunDetail = {
      ...runDetail,
      ...visibleRunListItem,
      events: [
        ...runDetail.events,
        {
          sequence: 4,
          kind: "runtime.failed",
          message: "hybrid discuss failed: model gateway failed: model transport failed",
          created_at: "2026-08-07T00:00:03Z",
          participants: [],
          payload: {},
        },
      ],
    };
    visibleConversationRuns = [visibleRunDetail];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);
    expect(within(stream).getByText("运行中断")).not.toBeNull();
    expect(within(stream).getByText(/中断前输出已保留/)).not.toBeNull();
    expect(within(stream).getByText(/model transport failed/)).not.toBeNull();
  });

  it("shows Codex-style chat replies with Kimi-style inline cluster actions", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);
    expect(within(stream).queryByText("Run accepted and queued.")).toBeNull();
    expect(within(stream).queryByText(/模式与角色/)).toBeNull();
    expect(within(stream).queryByText(/运行模式：/)).toBeNull();
    expect(within(stream).queryByText("model.started")).toBeNull();

    expect(within(stream).queryByText("正在实时刷新运行状态")).toBeNull();
    expect(within(stream).queryByRole("button", { name: /已记录 3 个关键步骤/ })).toBeNull();
    expect(within(stream).getByRole("status", { name: /Agent 集群/ })).not.toBeNull();
    expect(within(stream).queryByRole("button", { name: /生成了结果/ })).toBeNull();
    expect(within(stream).getByRole("button", { name: /文案生成 调用模型：qwen-max/ })).not.toBeNull();
    expect(within(stream).getByRole("button", { name: /文案生成 输出：得到一版可拍摄脚本文案/ })).not.toBeNull();
    expect(within(stream).getByRole("button", { name: /讨论结论：.*采用可拍摄性最高的方案/ })).not.toBeNull();
    await user.click(within(stream).getByRole("button", { name: /文案生成 输出：得到一版可拍摄脚本文案/ }));
    expect(within(stream).queryByText("任务已进入队列，等待 Worker 调度执行。")).toBeNull();
    const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });
    expect(within(drawer).queryByText("任务已进入队列，等待 Worker 调度执行。")).toBeNull();
    expect(within(drawer).queryByText("model.started")).toBeNull();
    expect(within(drawer).queryByText("模型请求已开始。")).toBeNull();
    expect(within(drawer).getByText("得到一版可拍摄脚本文案")).not.toBeNull();
    expect(within(drawer).getByText("调用模型")).not.toBeNull();
    expect(within(drawer).getByText("qwen-max")).not.toBeNull();
  });

  it("marks intermediate process outputs in labeled boxes while keeping the final reply merged", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getAllByText(/这是最终回复正文/).length).toBeGreaterThan(0);

    const processCards = Array.from(view.container.querySelectorAll(".process-intermediate-card"));
    const outputCard = processCards.find((card) => card.textContent?.includes("文案生成 输出"));
    expect(outputCard).not.toBeNull();
    expect(within(outputCard as HTMLElement).getByText("中间产物")).not.toBeNull();
    expect(outputCard?.textContent).toContain("得到一版可拍摄脚本文案");
  });

  it("renders agent process steps as an ordered timeline with concrete per-step details", async () => {
    const user = userEvent.setup();
    const timelineRunDetail = {
      ...runDetail,
      mode: "hybrid",
      events: [
        {
          sequence: 1,
          kind: "step.started",
          message: "step.started",
          created_at: conversationCreatedAt,
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "main_agent_plan",
          action: null,
          decision: null,
          payload: {
            mode: "hybrid",
            main_agent_model: "main",
            logical_model: "main",
            task: "选择运行模式、角色和模型。",
            roles: [
              { id: "copywriter", role: "文案生成", purpose: "execute", logical_model: "qwen-max", tools: [] },
              { id: "director", role: "导演", purpose: "expertise", logical_model: "deepseek-v4-flash", tools: [] },
            ],
            steps: [
              { id: "copywriting_step", agent: "copywriter", depends_on: [], final_synthesizer: false, tools: [] },
              { id: "discussion", agent: "director", depends_on: [], final_synthesizer: false, tools: [] },
            ],
          },
        },
        {
          sequence: 2,
          kind: "dispatch.started",
          message: "主 Agent 已拆解任务并派单。",
          created_at: conversationCreatedAt,
          actor: "main",
          participants: ["copywriter", "director"],
          tool_name: null,
          step_id: null,
          action: "dispatch",
          decision: null,
          payload: {
            instruction: "把中秋活动方案拆给文案生成和导演；文案负责活动文案，导演负责流程审查。",
          },
        },
        {
          sequence: 3,
          kind: "step.started",
          message: "文案生成开始处理活动文案。",
          created_at: "2026-08-07T00:00:01Z",
          actor: "copywriter",
          participants: [],
          tool_name: null,
          step_id: "copywriting_step",
          action: null,
          decision: null,
          payload: {
            task: "输出中秋节活动主题、流程和宣传文案。",
          },
        },
        {
          sequence: 4,
          kind: "model.started",
          message: "model.started",
          created_at: "2026-08-07T00:00:02Z",
          actor: "copywriter",
          participants: [],
          tool_name: null,
          step_id: "copywriting_step",
          action: null,
          decision: null,
          payload: {
            model: "qwen-max",
            provider: "qwen",
          },
        },
        {
          sequence: 5,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-08-07T00:00:03Z",
          actor: "copywriter",
          participants: [],
          tool_name: null,
          step_id: "copywriting_step",
          action: null,
          decision: null,
          payload: {},
          artifact: {
            id: "artifact-copywriter",
            kind: "markdown",
            title: "copywriter",
            text: "文案生成输出：中秋灯谜游园会，包含主题、流程、预算和宣传文案。",
          },
        },
        {
          sequence: 6,
          kind: "step.started",
          message: "导演开始审查流程。",
          created_at: "2026-08-07T00:00:04Z",
          actor: "director",
          participants: [],
          tool_name: null,
          step_id: "director_review_step",
          action: null,
          decision: null,
          payload: {
            task: "审查活动动线、安全和现场节奏。",
          },
        },
        {
          sequence: 7,
          kind: "discussion.completed",
          message: "文案生成和导演完成讨论。",
          created_at: "2026-08-07T00:00:05Z",
          actor: "director",
          participants: ["copywriter", "director"],
          tool_name: null,
          step_id: null,
          action: null,
          decision: "adopt",
          payload: {
            copywriter_opinion: "文案建议主打灯谜游园会。",
            director_opinion: "导演建议压缩签到环节，避免排队。",
            main_agent_judgement: "主 Agent 采纳灯谜游园会方案，并保留导演对动线的调整。",
            result: "采用灯谜游园会，压缩签到流程。",
          },
        },
      ],
      artifacts: [
        ...runDetail.artifacts,
        {
          id: "artifact-copywriter",
          kind: "markdown",
          title: "copywriter",
          text: "文案生成输出：中秋灯谜游园会，包含主题、流程、预算和宣传文案。",
        },
      ],
    };
    visibleRunListItem = { ...runListItem, mode: "hybrid" };
    visibleRunDetail = timelineRunDetail;
    visibleConversationRuns = [timelineRunDetail];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    const stream = screen.getByRole("region", { name: "主对话内容" });
    const mainPlan = within(stream).getByRole("button", { name: /主 Agent 接收任务：选择运行模式、角色和模型/ });
    const dispatch = within(stream).getByRole("button", { name: /主 Agent 派单给文案生成、导演/ });
    const copywriterStart = within(stream).getByRole("button", { name: /文案生成 接收任务：输出中秋节活动主题/ });
    const copywriterModel = within(stream).getByRole("button", { name: /文案生成 调用模型：qwen-max/ });
    const copywriterOutput = within(stream).getByRole("button", { name: /文案生成 输出：文案生成输出：中秋灯谜游园会/ });
    const directorStart = within(stream).getByRole("button", { name: /导演 接收任务：审查活动动线/ });
    const copywriterOpinion = within(stream).getByRole("button", { name: /文案生成 意见：文案建议主打灯谜游园会/ });
    const directorOpinion = within(stream).getByRole("button", { name: /导演 意见：导演建议压缩签到环节/ });
    const decision = within(stream).getByRole("button", { name: /主 Agent 裁决：主 Agent 采纳灯谜游园会方案/ });
    const ordered = [
      mainPlan,
      dispatch,
      copywriterStart,
      copywriterModel,
      copywriterOutput,
      directorStart,
      copywriterOpinion,
      directorOpinion,
      decision,
    ];
    ordered.reduce((previous, current) => {
      expect(previous.compareDocumentPosition(current) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      return current;
    });

    expect(within(stream).queryByRole("button", { name: /生成了结果/ })).toBeNull();

    await user.click(mainPlan);
    const planDrawer = await screen.findByRole("dialog", { name: "运行过程详情" });
    expect(within(planDrawer).getByText("执行者")).not.toBeNull();
    expect(within(planDrawer).getAllByText("主 Agent").length).toBeGreaterThan(0);
    expect(within(planDrawer).getByText("逻辑模型")).not.toBeNull();
    expect(within(planDrawer).getAllByText("main").length).toBeGreaterThan(0);
    await user.click(within(planDrawer).getByRole("button", { name: "关闭" }));

    await user.click(copywriterOutput);
    const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });
    expect(within(drawer).getByText("执行者")).not.toBeNull();
    expect(within(drawer).getAllByText("文案生成").length).toBeGreaterThan(0);
    expect(within(drawer).getByText("调用模型")).not.toBeNull();
    expect(within(drawer).getByText("qwen-max")).not.toBeNull();
    expect(within(drawer).getByText("输出内容")).not.toBeNull();
    expect(within(drawer).getAllByText(/中秋灯谜游园会/).length).toBeGreaterThan(0);

    await user.click(within(drawer).getByRole("button", { name: "关闭" }));
    await user.click(directorOpinion);
    const opinionDrawer = await screen.findByRole("dialog", { name: "运行过程详情" });
    expect(within(opinionDrawer).getByText("发言角色")).not.toBeNull();
    expect(within(opinionDrawer).getAllByText("导演").length).toBeGreaterThan(0);
    expect(within(opinionDrawer).getByText("导演意见")).not.toBeNull();
    expect(within(opinionDrawer).getByText("导演建议压缩签到环节，避免排队。")).not.toBeNull();
  });

  it("uses ordered artifacts for process rows instead of vague generated-result summaries", async () => {
    const user = userEvent.setup();
    const processRunDetail = {
      ...runDetail,
      events: [
        {
          sequence: 1,
          kind: "model.started",
          message: "model.started",
          created_at: "2026-08-07T00:00:01Z",
          actor: "copywriter",
          participants: [],
          tool_name: null,
          step_id: "copywriting_step",
          action: null,
          decision: null,
          payload: { model: "qwen-max" },
        },
        {
          sequence: 2,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-08-07T00:00:02Z",
          actor: "copywriter",
          participants: [],
          tool_name: null,
          step_id: "copywriting_step",
          action: null,
          decision: null,
          payload: {},
        },
        {
          sequence: 3,
          kind: "model.started",
          message: "model.started",
          created_at: "2026-08-07T00:00:03Z",
          actor: "director",
          participants: [],
          tool_name: null,
          step_id: "director_step",
          action: null,
          decision: null,
          payload: { model: "deepseek-v4-flash" },
        },
        {
          sequence: 4,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-08-07T00:00:04Z",
          actor: "director",
          participants: [],
          tool_name: null,
          step_id: "director_step",
          action: null,
          decision: null,
          payload: {},
        },
      ],
      artifacts: [
        {
          id: "artifact-copy",
          kind: "markdown",
          title: "script-draft",
          text: "文案生成输出：中秋活动脚本包含开场、互动和收尾。",
        },
        {
          id: "artifact-director",
          kind: "markdown",
          title: "director-review",
          text: "导演输出：压缩主持人串场，保留抽奖互动。",
        },
      ],
    };
    visibleRunDetail = processRunDetail;
    visibleConversationRuns = [processRunDetail];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    const stream = screen.getByRole("region", { name: "主对话内容" });
    const copywriterOutput = within(stream).getByRole("button", {
      name: /文案生成 输出：文案生成输出：中秋活动脚本包含开场、互动和收尾/,
    });
    const directorOutput = within(stream).getByRole("button", {
      name: /导演 输出：导演输出：压缩主持人串场，保留抽奖互动/,
    });
    expect(within(stream).queryByRole("button", { name: /完成阶段输出|生成了结果/ })).toBeNull();
    expect(copywriterOutput.compareDocumentPosition(directorOutput) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.click(directorOutput);
    const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });
    expect(within(drawer).getByText("执行者")).not.toBeNull();
    expect(within(drawer).getAllByText("导演").length).toBeGreaterThan(0);
    expect(within(drawer).getByText("调用模型")).not.toBeNull();
    expect(within(drawer).getByText("deepseek-v4-flash")).not.toBeNull();
    expect(within(drawer).getByText("输出内容")).not.toBeNull();
    expect(within(drawer).getByText("导演输出：压缩主持人串场，保留抽奖互动。")).not.toBeNull();
  });

  it("falls back to concrete artifact titles when upstream artifact text is generic", async () => {
    const user = userEvent.setup();
    const processRunDetail = {
      ...runDetail,
      events: [
        {
          sequence: 1,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-08-07T00:00:02Z",
          actor: "copywriter",
          participants: [],
          tool_name: null,
          step_id: "copywriting_step",
          action: null,
          decision: null,
          payload: {},
          artifact: {
            id: "artifact-generic-copy",
            kind: "markdown",
            title: "中秋活动文案初稿",
            text: "已生成一个可查看的结果或中间产物。",
          },
        },
      ],
      artifacts: [
        {
          id: "artifact-generic-copy",
          kind: "markdown",
          title: "中秋活动文案初稿",
          text: "已生成一个可查看的结果或中间产物。",
        },
      ],
    };
    visibleRunDetail = processRunDetail;
    visibleConversationRuns = [processRunDetail];

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));
    const stream = screen.getByRole("region", { name: "主对话内容" });
    const outputRow = within(stream).getByRole("button", { name: /文案生成 输出：中秋活动文案初稿/ });
    expect(within(stream).queryByText(/已生成一个可查看的结果或中间产物/)).toBeNull();

    await user.click(outputRow);
    const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });
    expect(within(drawer).getByText("产物标题")).not.toBeNull();
    expect(within(drawer).getAllByText("中秋活动文案初稿").length).toBeGreaterThan(0);
    expect(within(drawer).queryByText(/已生成一个可查看的结果或中间产物/)).toBeNull();
  });

  it("shows localized process summaries with participating roles instead of raw event codes", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationOpenButtonName }));

    const stream = screen.getByRole("region", { name: "主对话内容" });
    await user.click(within(stream).getByRole("button", { name: /讨论结论：.*采用可拍摄性最高的方案/ }));
    const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });

    expect(within(drawer).getByText("参与者")).not.toBeNull();
    expect(within(drawer).getByText("导演、文案生成、剪辑师")).not.toBeNull();
    expect(within(drawer).queryByText(/生成了结果/)).toBeNull();
    expect(within(drawer).getAllByText(/多角色完成讨论/).length).toBeGreaterThan(0);
    expect(within(drawer).getAllByText(/采用可拍摄性最高的方案/).length).toBeGreaterThan(0);
    expect(within(drawer).getByText("导演认为要优先可拍摄性。")).not.toBeNull();
    expect(within(drawer).getByText("文案建议强化开头钩子。")).not.toBeNull();
    expect(within(drawer).getByText("剪辑师建议三段式节奏。")).not.toBeNull();
    expect(within(drawer).getByText("主 Agent 选择可拍摄性最高且风险最低的方案。")).not.toBeNull();
    expect(within(drawer).getAllByText("执行者").length).toBeGreaterThan(0);
    expect(within(drawer).getByText("参与者")).not.toBeNull();
    expect(within(drawer).getByText("导演、文案生成、剪辑师")).not.toBeNull();
    expect(within(drawer).queryByText("artifact.created")).toBeNull();
  });


  it("opens conversation history as a right drawer", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    const shell = document.querySelector(".app-shell");
    const chatConsole = document.querySelector(".chat-console");

    await user.click(screen.getByRole("button", { name: "打开导航栏" }));
    expect(shell?.className).toContain("mobile-nav-open");

    const historyTrigger = screen.getByRole("button", { name: "打开历史对话" });
    expect(historyTrigger.className).toContain("mobile-nav-trigger");
    expect(historyTrigger.className).toContain("conversation-drawer-trigger");

    await user.click(historyTrigger);
    expect(shell?.className).not.toContain("mobile-nav-open");
    expect(chatConsole?.className).toContain("history-drawer-open");
    expect(screen.getByRole("navigation", { name: "会话导航" })).not.toBeNull();
    const conversationOpenButton = screen.getByRole("button", { name: conversationOpenButtonName });
    expect(conversationOpenButton).not.toBeNull();
    expect(conversationOpenButton.querySelector(".conversation-title-text")?.textContent).toBe(conversationHistoryTitle);
    expect(screen.getByText("全选可删")).not.toBeNull();
    expect(screen.getByRole("button", { name: /批量删除已选会话 0 条/ })).not.toBeNull();
    expect(screen.getByText("删除已选（0）")).not.toBeNull();
    expect(screen.getByText(conversationHistoryTitle)).not.toBeNull();
    expect(screen.queryByText("22222222")).toBeNull();
    expect(screen.getAllByRole("button", { name: "关闭历史对话" }).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "打开导航栏" }));
    expect(chatConsole?.className).not.toContain("history-drawer-open");
  });
  it("keeps quick mode under main-agent auto routing without forcing direct", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.type(screen.getByPlaceholderText(/输入消息/), "你好，直接回复我。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "你好，直接回复我。",
        mode: "auto",
      },
    });
    expect(screen.queryByRole("dialog", { name: "运行模式确认" })).toBeNull();
  });

  it("selects the chat mode from the compact entry panel before sending", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "讨论" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "请让多个角色评审这个方案。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "请让多个角色评审这个方案。",
        mode: "discuss",
      },
    });
  });

  it("uses a mode keyword from the new-chat input without requiring numeric choices", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "自动" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "直连" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "派单" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "讨论" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "混合" })).not.toBeNull();

    await user.type(screen.getByPlaceholderText(/输入消息/), "讨论 请让多个角色评审这个方案。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "请让多个角色评审这个方案。",
        mode: "discuss",
      },
    });
  });

  it("does not ask again when a manually selected mode is returned as backend clarification", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "讨论" }));
    await user.type(screen.getByPlaceholderText(/输入消息/), "这个任务不应该二次确认。");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(screen.queryByRole("dialog", { name: "运行模式确认" })).toBeNull();
    expect(await screen.findByText(/不再重复确认模式/)).not.toBeNull();

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/runs/${runId}/choose-mode`)).toMatchObject({
        method: "POST",
        body: {
          mode: "discuss",
          decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
          version: 1,
          operator_note: "用户已在新对话入口明确选择该模式。",
        },
      }),
    );
  });

  it("uploads an archive as a normal attachment first and installs it as a skill only after explicit action", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    const file = new File(["PK\x03\x04"], "uploaded-skill.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("上传文件或 Skill 压缩包"), file);

    expect(await screen.findByText("压缩包附件")).not.toBeNull();
    expect(screen.getByText("uploaded-skill.zip")).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/runs/attachments/upload")).toMatchObject({
      method: "POST",
    });
    expect(requests.find((request) => request.path === "/api/v1/admin/skills/upload")).toBeUndefined();

    await user.click(screen.getByRole("button", { name: "作为 Skill 安装" }));

    expect(await screen.findByText("Skill 压缩包已扫描，等待确认")).not.toBeNull();
    expect(screen.getByText("uploaded_skill")).not.toBeNull();
    expect(screen.getByText(/tool:filesystem\.read/)).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/skills/upload")).toMatchObject({
      method: "POST",
    });

    await user.click(screen.getByRole("button", { name: "确认安装 Skill" }));
    expect(await screen.findByText("Skill 已安装并启用")).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/skills/skill-uploaded-from-chat/approve")).toMatchObject({
      method: "POST",
    });
  });

  it("uploads an image attachment from chat and submits its attachment id with the run", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    const file = new File(["image-bytes"], "screen.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("上传文件或 Skill 压缩包"), file);
    expect(await screen.findByText("图片附件")).not.toBeNull();
    expect(screen.getByText("screen.png")).not.toBeNull();

    await user.type(screen.getByPlaceholderText(/输入消息/), "请根据图片说明问题");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByRole("link", { name: "查看运行详情" });
    expect(requests.find((request) => request.path === "/api/v1/runs")).toMatchObject({
      method: "POST",
      body: {
        message: "请根据图片说明问题",
        attachment_ids: ["att_0123456789abcdef0123456789abcdef"],
      },
    });
  });
  it("encodes non-ascii attachment filenames before sending upload headers", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    const fileName = "截图 方案.png";
    const file = new File(["image-bytes"], fileName, { type: "image/png" });
    await user.upload(screen.getByLabelText("上传文件或 Skill 压缩包"), file);

    expect(await screen.findByText("图片附件")).not.toBeNull();
    expect(screen.getByText(fileName)).not.toBeNull();
    const uploadRequest = requests.find((request) => request.path === "/api/v1/runs/attachments/upload");
    expect(uploadRequest?.headers["x-agent-hub-filename-encoding"]).toBe("percent");
    expect(uploadRequest?.headers["x-agent-hub-filename"]).toBe(encodeURIComponent(fileName));
    expect(/^[\x00-\x7F]*$/.test(uploadRequest?.headers["x-agent-hub-filename"] ?? "")).toBe(true);
  });

  it("allows common archive and document attachments from chat", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    const uploadInput = screen.getByLabelText("上传文件或 Skill 压缩包");
    const accept = uploadInput.getAttribute("accept") ?? "";
    expect(accept).toContain(".rar");
    expect(accept).toContain(".7z");
    expect(accept).toContain(".tar.gz");

    const file = new File(["archive-bytes"], "project-source.rar", { type: "application/vnd.rar" });
    await user.upload(uploadInput, file);

    expect(await screen.findByText("压缩包附件")).not.toBeNull();
    expect(screen.getByText("project-source.rar")).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/runs/attachments/upload")).toMatchObject({
      method: "POST",
    });
  });

  it("deletes a finished conversation from the conversation list", async () => {
    const user = userEvent.setup();
    visibleRunListItem = { ...runListItem, status: "cancelled" };
    visibleRunListItems = [visibleRunListItem];
    visibleRunDetail = { ...runDetail, status: "cancelled" };
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("button", { name: conversationDeleteButtonName })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: conversationDeleteButtonName }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/admin/runs/${runId}` && request.method === "DELETE"))
        .toBeTruthy(),
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: conversationDeleteButtonName })).toBeNull());
  });

  it("bulk selects finished conversations and deletes them through one batch API call", async () => {
    const user = userEvent.setup();
    visibleRunListItems = [
      { ...runListItem, status: "cancelled" },
      secondRunListItem,
    ];
    visibleRunListItem = visibleRunListItems[0];
    visibleRunDetail = { ...runDetail, status: "cancelled" };
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("checkbox", { name: "Select all deletable conversations" })).not.toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Select all deletable conversations" }));
    await user.click(screen.getByRole("button", { name: /批量删除已选会话/ }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/runs/bulk-delete")).toMatchObject({
        method: "POST",
        body: { ids: [runId, secondRunId] },
      }),
    );
  });

  it("shows temporary agent approval above the composer and lets the user revise it", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/" />);

    await waitFor(() => expect(view.container.querySelector(".chat-composer")).not.toBeNull());
    await openRunConfig(user);
    await user.selectOptions(screen.getAllByRole("combobox")[1], "short-video-dispatch");
    const composer = view.container.querySelector(".chat-composer") as HTMLFormElement;
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "make this into a web page");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText("Temporary Web Engineer")).not.toBeNull();
    expect(screen.queryByRole("dialog", { name: "临时 Agent 确认提醒" })).toBeNull();
    await user.clear(composer.querySelector("textarea") as HTMLTextAreaElement);
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "3 do not add an engineer yet");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);

    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/runs/${runId}/revise-temporary-agent`)).toMatchObject({
        method: "POST",
        body: {
          decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
          version: 1,
          feedback: "do not add an engineer yet",
        },
      }),
    );
  });

  it("accepts a temporary agent and can persist it as a normal agent", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/" />);

    await waitFor(() => expect(view.container.querySelector(".chat-composer")).not.toBeNull());
    await openRunConfig(user);
    await user.selectOptions(screen.getAllByRole("combobox")[1], "short-video-dispatch");
    const composer = view.container.querySelector(".chat-composer") as HTMLFormElement;
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "make this into a web page");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);

    const stream = screen.getByRole("region", { name: "主对话内容" });
    expect(within(stream).getByText(/主 Agent 已生成角色和提示词/)).not.toBeNull();
    expect(within(stream).getByText(/主 Agent 会按角色能力、任务要求和模型并发情况自动选择模型/)).not.toBeNull();
    expect(within(stream).queryByLabelText("运行模型")).toBeNull();
    expect(within(stream).queryByText(/建议模型\/API：coder/)).toBeNull();
    await user.clear(composer.querySelector("textarea") as HTMLTextAreaElement);
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "1");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);
    await waitFor(() =>
      expect(requests.find((request) => request.path === `/api/v1/runs/${runId}/approve-temporary-agent`)).toMatchObject({
        body: {
          decision_token: "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
          version: 1,
        },
      }),
    );
    await user.clear(composer.querySelector("textarea") as HTMLTextAreaElement);
    await user.type(composer.querySelector("textarea") as HTMLTextAreaElement, "保存");
    await user.click(composer.querySelector('button[type="submit"]') as HTMLButtonElement);

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/agents" && request.method === "POST")).toMatchObject({
        body: {
          id: "temp-web-engineer",
          name: "Temporary Web Engineer",
          role: "Web Engineer",
          prompt: "把方案落成网页并说明验证步骤。",
          model: "main",
          skills: ["frontend"],
        },
      }),
    );
  });

  it("keeps a clear mobile hierarchy for chat sessions, content, and run settings", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    expect(screen.getByRole("navigation", { name: "会话导航" })).not.toBeNull();
    expect(screen.getByRole("region", { name: "主对话内容" })).not.toBeNull();
    expect(screen.getByRole("button", { name: /打开本次运行配置/ })).not.toBeNull();
    await openRunConfig(user);
    expect(screen.getByRole("group", { name: "本次运行设置" })).not.toBeNull();
    expect(screen.getByText("本次运行设置")).not.toBeNull();
  });


  it("loads a referenced conversation by id from the chat page", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("heading", { name: "对话与进化" })).not.toBeNull();
    await openRunConfig(user);
    await user.type(screen.getByLabelText("参考会话 ID"), "conv-previous");
    await user.click(screen.getByRole("button", { name: "读取参考会话" }));

    expect(await screen.findByText("conv-previous")).not.toBeNull();
    expect(screen.getByText(/已读取 1 条运行/)).not.toBeNull();
    expect(screen.getAllByText(runDetail.request).length).toBeGreaterThan(0);
  });



  it("shows evolution records in the evolution module and creates a new run", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/evolution" />);

    expect(await screen.findByRole("heading", { name: "进化" })).not.toBeNull();
    expect(screen.getByText(/普通问答、方案规划和对话上下文压缩属于对话框架/)).not.toBeNull();
    const dashboard = screen.getByRole("region", { name: "进化执行看板" });
    expect(dashboard).not.toBeNull();
    await waitFor(() => expect(within(dashboard).getByText("总任务").closest("div")?.textContent).toContain("1"));
    expect(within(dashboard).getByText("运行中").closest("div")?.textContent).toContain("1");
    expect(within(dashboard).getByText("待审批").closest("div")?.textContent).toContain("0");
    expect(within(dashboard).getByText("待执行").closest("div")?.textContent).toContain("1");
    const records = await screen.findByRole("region", { name: "进化任务" });
    expect(await within(records).findByText("Darwin Skill 迭代")).not.toBeNull();
    expect(within(records).getByText("agent-main-m3")).not.toBeNull();
    expect(within(records).getByText("run_next_round")).not.toBeNull();
    expect(within(records).getByText(/第 1 轮/)).not.toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "搜索进化任务" }), "不存在的任务");
    expect(within(records).queryByText("Darwin Skill 迭代")).toBeNull();
    expect(screen.getByText("没有符合筛选条件的进化任务。")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "清空进化筛选" }));
    expect(await within(records).findByText("Darwin Skill 迭代")).not.toBeNull();
    await user.selectOptions(screen.getByLabelText("按进化状态筛选"), "pending_approval");
    expect(within(records).queryByText("Darwin Skill 迭代")).toBeNull();
    await user.selectOptions(screen.getByLabelText("按进化状态筛选"), "needs_action");
    expect(await within(records).findByText("Darwin Skill 迭代")).not.toBeNull();
    await user.selectOptions(screen.getByLabelText("按进化状态筛选"), "all");

    await user.clear(screen.getByLabelText("任务名称"));
    await user.type(screen.getByLabelText("任务名称"), "学术研究进化");
    await user.clear(screen.getByLabelText("目标"));
    await user.type(screen.getByLabelText("目标"), "迭代发现论文创新点。");
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    await waitFor(() => expect(createdEvolutionRun?.title).toBe("学术研究进化"));
    expect(requests.find((request) => request.path === "/api/v1/admin/evolution-runs" && request.method === "POST")).toMatchObject({
      body: {
        title: "学术研究进化",
        objective: "迭代发现论文创新点。",
        kind: "skill_optimization",
        baseline_agent_id: "agent-main-m3",
        evaluator_agent_id: "agent-evaluator",
        approval_policy: "ask",
        iteration_policy: "score_gated",
        memory_policy: "summarize_between_rounds",
      },
    });

    await user.clear(screen.getByLabelText(/审批基准 agent/));
    await user.type(screen.getByLabelText(/审批基准 agent/), "agent-main-strong");
    await user.clear(screen.getByLabelText(/审批评测 agent/));
    await user.type(screen.getByLabelText(/审批评测 agent/), "agent-evaluator-strong");
    await user.clear(screen.getByLabelText(/审批备注/));
    await user.type(screen.getByLabelText(/审批备注/), "更换基准后再进入首轮。");
    await user.click(await screen.findByRole("button", { name: "审批通过" }));
    await waitFor(() => expect(createdEvolutionRun?.status).toBe("running"));
    expect(requests.find((request) => request.path.endsWith(`/evolution-runs/${createdEvolutionRun?.id}/approve`))).toMatchObject({
      body: {
        approved: true,
        baseline_agent_id: "agent-main-strong",
        evaluator_agent_id: "agent-evaluator-strong",
        note: "更换基准后再进入首轮。",
      },
    });

    const createdRunCard = screen.getByRole("heading", { name: "学术研究进化" }).closest("article");
    expect(createdRunCard).not.toBeNull();
    await user.click(within(createdRunCard as HTMLElement).getByRole("button", { name: "生成执行包" }));
    await waitFor(() => expect(screen.getByText("学术研究进化 / round 1")).not.toBeNull());
    expect(screen.getByText(/固定评测集比较基准和候选/)).not.toBeNull();
    expect(screen.getByText(/score_before/)).not.toBeNull();
    expect(requests.find((request) => request.path.endsWith(`/evolution-runs/${createdEvolutionRun?.id}/next-round-plan`))).toMatchObject({
      method: "GET",
    });

    await user.click(within(createdRunCard as HTMLElement).getByRole("button", { name: "启动执行" }));
    await waitFor(() => expect(screen.getByText(/已启动第 1 轮执行/)).not.toBeNull());
    expect(screen.getByText(/44444444-4444-4444-8444-444444444444/)).not.toBeNull();
    expect(screen.getByRole("link", { name: "打开执行运行" }).getAttribute("href")).toBe(
      "/runs/44444444-4444-4444-8444-444444444444",
    );
    expect(requests.find((request) => request.path.endsWith(`/evolution-runs/${createdEvolutionRun?.id}/execute-next-round`))).toMatchObject({
      method: "POST",
    });

    await user.click(screen.getByRole("button", { name: "导入执行结果" }));
    await waitFor(() => expect(createdEvolutionRun?.rounds[0]?.candidate_summary).toBe("执行运行产物已导入。"));
    expect(screen.getByText(/第 1 轮：执行结果导入/)).not.toBeNull();
    expect(requests.find((request) => request.path.endsWith(`/evolution-runs/${createdEvolutionRun?.id}/execution-runs/44444444-4444-4444-8444-444444444444/ingest`))).toMatchObject({
      method: "POST",
    });
    await user.clear(screen.getByLabelText("改动维度"));
    await user.type(screen.getByLabelText("改动维度"), "反例覆盖");
    await user.clear(screen.getByLabelText("候选摘要"));
    await user.type(screen.getByLabelText("候选摘要"), "扩展失败样例并压缩提示词。");
    await user.clear(screen.getByLabelText("前分数"));
    await user.type(screen.getByLabelText("前分数"), "76");
    await user.clear(screen.getByLabelText("后分数"));
    await user.type(screen.getByLabelText("后分数"), "74");
    await user.click(screen.getByLabelText("发现回归"));
    await user.selectOptions(screen.getByLabelText("候选接收"), "reject");
    await user.clear(screen.getByLabelText("评审说明"));
    await user.type(screen.getByLabelText("评审说明"), "反例集失败，要求回滚候选版本。");
    await user.clear(screen.getByLabelText("产物引用"));
    await user.type(screen.getByLabelText("产物引用"), "artifact://eval/report-1\nartifact://candidate/patch-1");
    await user.clear(screen.getByLabelText("Token 消耗"));
    await user.type(screen.getByLabelText("Token 消耗"), "4096");
    await user.clear(screen.getByLabelText("耗时秒"));
    await user.type(screen.getByLabelText("耗时秒"), "180");
    await user.click(screen.getByRole("button", { name: "登记轮次" }));

    await waitFor(() => expect(createdEvolutionRun?.next_action).toBe("rollback_candidate"));
    expect(requests.find((request) => request.path.endsWith(`/evolution-runs/${createdEvolutionRun?.id}/rounds`))).toMatchObject({
      body: {
        changed_dimension: "反例覆盖",
        candidate_summary: "扩展失败样例并压缩提示词。",
        score_before: 76,
        score_after: 74,
        tests_passed: true,
        regression_detected: true,
        accepted: false,
        judge_summary: "反例集失败，要求回滚候选版本。",
        artifact_refs: ["artifact://eval/report-1", "artifact://candidate/patch-1"],
        tokens_used: 4096,
        elapsed_seconds: 180,
      },
    });
  }, 10_000);
  it("distinguishes server environment channel values from cleared page settings", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/channels" />);

    expect(await screen.findByRole("heading", { name: "通道连接" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /飞书/ }));

    expect(screen.getByText("当前来源：服务器环境")).not.toBeNull();
    expect(screen.getByText(/服务器环境已配置，页面清空不会删除/)).not.toBeNull();
  });
  it("lets configured channel settings be edited and cleared", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/channels" />);

    expect(await screen.findByRole("heading", { name: "通道连接" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /自定义 Webhook/ }));

    await user.type(screen.getByLabelText(/Webhook Token/), "saved-token");
    await user.click(screen.getByRole("button", { name: "保存通道配置" }));
    await waitFor(() => expect(screen.getByText("通道配置已保存，可继续修改或清空。面板已刷新最新状态。")));
    expect(screen.getAllByText("已接通").length).toBeGreaterThan(0);
    expect(requests.find((request) => request.path === "/api/v1/admin/channels/custom_webhook/config" && request.method === "POST")).toMatchObject({
      body: { values: { CUSTOM_WEBHOOK_TOKEN: "saved-token" } },
    });

    await user.click(screen.getByRole("button", { name: "清空当前通道配置" }));
    await waitFor(() => expect(screen.getByText("通道配置已清空。需要重新填写后才会接通。")));
    expect(screen.getByText(/还缺少配置：CUSTOM_WEBHOOK_TOKEN/)).not.toBeNull();
    expect(requests.find((request) => request.path === "/api/v1/admin/channels/custom_webhook/config" && request.method === "DELETE")).toBeTruthy();
  });
  it("shows MCP, memory, and modular log pages", async () => {
    const user = userEvent.setup();

    render(<TestApp initialPath="/mcp" />);
    expect(await screen.findByText("Filesystem MCP")).not.toBeNull();
    expect(screen.getByText("healthy")).not.toBeNull();

    cleanup();
    render(<TestApp initialPath="/memory" />);
    expect(await screen.findByText("project-policy")).not.toBeNull();
    expect(screen.getByText("tenant")).not.toBeNull();
    expect(await screen.findByText("热度 0.82")).not.toBeNull();
    expect(screen.getByText("已锁定")).not.toBeNull();
    expect(screen.getByText("项目 cube-agent")).not.toBeNull();
    expect(screen.getByText("摘要 week")).not.toBeNull();
    expect(screen.getByText("召回 3 次")).not.toBeNull();

    cleanup();
    const logsView = render(<TestApp initialPath="/logs" />);
    expect(await screen.findByRole("heading", { name: "日志" })).not.toBeNull();
    const logsMain = within(logsView.container.querySelector("main") as HTMLElement);
    expect(logsMain.getByRole("link", { name: /审计日志/ })).not.toBeNull();
    expect(logsMain.getByRole("link", { name: /模型配置与调用错误/ })).not.toBeNull();
    expect(logsMain.getByRole("link", { name: /模式运行错误/ })).not.toBeNull();

    cleanup();
    render(<TestApp initialPath="/logs/model" />);
    expect(await screen.findByRole("heading", { name: "模型配置与调用错误", level: 2 })).not.toBeNull();
    expect(await screen.findByText("provider returned status=401")).not.toBeNull();
    expect(screen.getByText("anthropic preflight latency is high")).not.toBeNull();
    expect(screen.getByRole("checkbox", { name: "Select all logs in current module" })).not.toBeNull();
    expect(screen.getByRole("checkbox", { name: "Select log model-error-1" })).not.toBeNull();
    expect(screen.queryByText("dispatch runtime failed")).toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "搜索日志" }), "anthropic");
    expect(screen.queryByText("provider returned status=401")).toBeNull();
    expect(screen.getByText("anthropic preflight latency is high")).not.toBeNull();

    await user.clear(screen.getByRole("searchbox", { name: "搜索日志" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "日志级别" }), "error");
    expect(screen.getByText("provider returned status=401")).not.toBeNull();
    expect(screen.queryByText("anthropic preflight latency is high")).toBeNull();

    await user.selectOptions(screen.getByRole("combobox", { name: "日志级别" }), "all");
    await user.type(screen.getByRole("textbox", { name: "按日志来源筛选" }), "models.probe");
    const logTable = screen.getByRole("table", { name: "模型配置与调用错误列表" });
    expect(within(logTable).getByText("anthropic preflight latency is high")).not.toBeNull();
    expect(within(logTable).queryByText("provider returned status=401")).toBeNull();

    cleanup();
    render(<TestApp initialPath="/logs/audit" />);
    expect(await screen.findByRole("heading", { name: "审计日志", level: 2 })).not.toBeNull();
    expect(screen.getByText("对话提交")).not.toBeNull();
    expect(screen.getByText(/用户 11111111-1111-4111-8111-111111111111 \/ 对话 conv-audit-user-1/)).not.toBeNull();
    await user.type(screen.getByRole("textbox", { name: "按日志详情筛选" }), "conv-audit-user-1");
    const auditTable = screen.getByRole("table", { name: "审计日志列表" });
    expect(within(auditTable).getByText("对话提交")).not.toBeNull();
    expect(within(auditTable).queryByText("config.publish")).toBeNull();
    cleanup();
    render(<TestApp initialPath="/logs/audit?details=auth.login" />);
    const loginAuditTable = await screen.findByRole("table", { name: "审计日志列表" });
    expect(within(loginAuditTable).getAllByText("auth.login").length).toBeGreaterThan(0);
    expect(within(loginAuditTable).queryByText("对话提交")).toBeNull();

    cleanup();
    render(<TestApp initialPath="/logs/audit?details=run.submit" />);
    const conversationAuditTable = await screen.findByRole("table", { name: "审计日志列表" });
    expect(within(conversationAuditTable).getByText("对话提交")).not.toBeNull();
    expect(within(conversationAuditTable).queryByText("auth.login")).toBeNull();
  });

  it("shows Hermes learning by time and conversation id with detail confirmation", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("table", { name: /Hermes/ })).not.toBeNull();
    expect(screen.queryByText("请求 Hermes 推荐")).toBeNull();
    expect(screen.queryByText("推荐结果")).toBeNull();
    expect(screen.getByText("conv-architecture-1")).not.toBeNull();
    expect(screen.getByText("2026-08-07T00:04:00Z")).not.toBeNull();
    await user.click(screen.getByRole("link", { name: /conv-architecture-1/ }));

    expect(await screen.findByText(hermesInsight.summary)).not.toBeNull();
    expect(screen.getByText(hermesInsight.lesson)).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /确认/ }));

    await waitFor(() => expect(screen.getByText("2026-08-07T00:05:00Z")).not.toBeNull());
  });

  it("separates Hermes conversation memory from scheduler observations", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes?category=scheduler" />);

    const table = await screen.findByRole("table", { name: /Hermes/ });
    expect(within(table).getByRole("cell", { name: "调度观察" })).not.toBeNull();
    expect(within(table).getByText("conv-workflow-2")).not.toBeNull();
    expect(within(table).queryByText("conv-architecture-1")).toBeNull();

    await user.click(screen.getByRole("checkbox", { name: "Select all visible Hermes learning records" }));
    await user.click(screen.getByRole("button", { name: /批量确认待确认学习/ }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/hermes/bulk-confirm")).toMatchObject({
        method: "POST",
        body: { ids: ["hermes_run_22222222222222222222222222222222"] },
      }),
    );
  });
  it("bulk selects Hermes learning records and confirms them through one batch API call", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("checkbox", { name: "Select all visible Hermes learning records" })).not.toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Select all visible Hermes learning records" }));
    await user.click(screen.getByRole("button", { name: /批量确认待确认学习/ }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/hermes/bulk-confirm")).toMatchObject({
        method: "POST",
        body: { ids: ["hermes_run_22222222222222222222222222222222", "hermes_run_11111111111111111111111111111111"] },
      }),
    );
  });

  it("confirms a Hermes learning record directly from the table", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("table", { name: /Hermes/ })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "确认 Hermes 学习 hermes_run_11111111111111111111111111111111" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/hermes/hermes_run_11111111111111111111111111111111/confirm")).toMatchObject({
        method: "POST",
      }),
    );
  });

  it("clears Hermes bulk selection when select all is clicked again", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    const selectAll = await screen.findByRole("checkbox", { name: "Select all visible Hermes learning records" });
    const confirmButton = screen.getByRole("button", { name: /批量确认待确认学习/ }) as HTMLButtonElement;
    const deleteButton = screen.getByRole("button", { name: /批量删除已选学习/ }) as HTMLButtonElement;

    expect(confirmButton.disabled).toBe(true);
    expect(deleteButton.disabled).toBe(true);
    await user.click(selectAll);
    expect(screen.getByText("当前结果已选 2")).not.toBeNull();
    expect(confirmButton.disabled).toBe(false);
    expect(deleteButton.disabled).toBe(false);
    await user.click(selectAll);

    expect(screen.getByText("当前结果已选 0")).not.toBeNull();
    expect(confirmButton.disabled).toBe(true);
    expect(deleteButton.disabled).toBe(true);
  });

  it("bulk deletes selected Hermes learning records through one batch API call", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("checkbox", { name: "Select all visible Hermes learning records" })).not.toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Select all visible Hermes learning records" }));
    await user.click(screen.getByRole("button", { name: /批量删除已选学习/ }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/hermes/bulk-delete")).toMatchObject({
        method: "POST",
        body: { ids: ["hermes_run_22222222222222222222222222222222", "hermes_run_11111111111111111111111111111111"] },
      }),
    );
    await waitFor(() => expect(screen.queryByText("conv-architecture-1")).toBeNull());
    expect(screen.queryByText("conv-workflow-2")).toBeNull();
  });

  it("deletes a Hermes learning record from the table", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("table", { name: /Hermes/ })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "删除 Hermes 学习 hermes_run_11111111111111111111111111111111" }));

    await waitFor(() =>
      expect(requests.find((request) => request.path === "/api/v1/admin/hermes/hermes_run_11111111111111111111111111111111")).toMatchObject({
        method: "DELETE",
      }),
    );
    await waitFor(() => expect(screen.queryByText("conv-architecture-1")).toBeNull());
    expect(screen.getByText("conv-workflow-2")).not.toBeNull();
  });

  it("shows detailed API errors on run list loading failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse(
            { error: { code: "service_unavailable", message: "database is not ready" } },
            { status: 503, headers: { "X-Error-ID": "err_123" } },
          );
        }
        if (path === "/api/v1/admin/settings") return jsonResponse(settings);
        if (path === "/api/v1/admin/agents") return jsonResponse(agents);
        if (path === "/api/v1/admin/workflows") return jsonResponse(workflows);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath="/" />);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "会话列表加载失败: database is not ready (service_unavailable, HTTP 503, error err_123)",
    );
  });
});
