import { createRoot } from "react-dom/client";

import { conversationMessages, MessageBody } from "../../src/pages/RunsPage";
import type { RunDetail } from "../../src/api/client";
import { useVisibleViewport } from "../../src/app/useVisibleViewport";
import { TestApp } from "../../src/app/router";
import "../../src/styles.css";
import "../../src/conversation-workspace.css";

export const prose = "## 调查结果\n\n第一段说明当前执行结果，保留清晰的阅读节奏。**重点信息**和 `config.json` 应当容易识别。\n\n第二段说明后续安排，不应与上一段挤在一起。\n\n### 下一步\n\n3. 检查配置\n   - 对照输入\n   - 核对结果\n4. 完成验证\n\n> 引用说明：正文和辅助信息保持明确层级。";
export const source = 'const message = "' + "long-source-value-".repeat(30) + '";\n'
  + Array.from({ length: 65 }, (_, i) => `console.log("line-${i}", message);`).join("\n");
export const mixed = `${prose}\n\n### 对照记录\n\n| 项目 | 当前结果 | 下一步 |\n| --- | --- | --- |\n| 配置 | 已读取，保留原始内容，长说明在单元格内换行。 | 核对输入 |\n| 数据 | ${"long_identifier_".repeat(12)} | 验证边界 |\n\n### 示例代码\n\n\`\`\`javascript\n${source}\n\`\`\`\n\n正文结尾可达。`;

const count = Number(new URLSearchParams(location.search).get("count")) || 16;
const payload = Object.fromEntries(Array.from({ length: count }, (_, i) => [`field_${i}`,
  i === 0 ? mixed : i === 1 ? new URLSearchParams(location.search).has("short")
    ? { run_id: "1d3d0a86-adba-4e9a-a47c-0123456789ab", source: "hybrid_runtime", sequence: 22,
      parent_run_id: "1d3d0a86-adba-4e9a-a47c-0123456789ab", schema_version: 1 }
    : { entries: Array.from({ length: 70 }, (_, n) => ({ n, value: "长 JSON 原始内容" })) }
    : i === count - 1 ? "TAIL_REACHABLE" : `完整活动内容：第 ${i} 项说明，不应受到同列长代码或 JSON 挤压。`,
]));
const longText = `## 长回复\n\n**关键结论**与 \`settings.json\`：${"长回复正文需要保留一致的文字层级。".repeat(80)}末尾完整内容。`;
const artifact = { id: "reply", title: "main", kind: "markdown",
  text: new URLSearchParams(location.search).has("long") ? longText : mixed };
if (new URLSearchParams(location.search).has("machine")) {
  artifact.text = JSON.stringify({ result: { summary: "Workspace contains 0 files.", workspace_files: [],
    bundle_download_url: "/api/v1/workspaces/projects/example/sessions/main/bundle/download" } });
}
const run = {
  id: "55555555-5555-4555-8555-555555555555", status: "completed", mode: "dispatch", version: 1,
  conversation_id: "layout-conversation", request: "检查结果和排版", created_at: "2026-10-22T00:00:00Z",
  queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0", explicit_details: {},
  events: [{ sequence: 1, kind: "artifact.created", message: "LAYOUT_ACTIVITY", summary: "LAYOUT_ACTIVITY",
    created_at: "2026-10-22T00:00:00Z", actor: "writer", participants: [], payload, artifact }],
  artifacts: [artifact], failure_diagnostics: [], tool_lifecycle: [],
  model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
    requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] },
} as unknown as RunDetail;

if (new URLSearchParams(location.search).has("feedback")) {
  run.events.push(...["Object of type mappingproxy is not JSON serializable",
    'EXPLICIT_CAPABILITY_CALLS_JSON=[{"name":"project.verify","arguments":{}}]'].map((message, index) => ({
      sequence: index + 2, kind: "message.created", message, actor: "writer", participants: [], payload: {}, created_at: run.created_at!,
    })));
}

if (new URLSearchParams(location.search).has("files")) {
  const codeFile = new URLSearchParams(location.search).has("codefile");
  run.artifacts.push({
    id: "delivery-file", kind: "tool_result", title: "implementer", text: codeFile ? source : null,
    filename: codeFile ? "scroll-preview.js" : "项目交付-20261022-完整源代码与验收记录-frontend-backend-documentation.zip",
    mime_type: codeFile ? "text/javascript" : "application/zip", size_bytes: 4096,
    download_url: "/api/v1/runs/55555555-5555-4555-8555-555555555555/artifacts/delivery-file/download",
    presentation: "final_attachment",
  });
}

// Supply only local fixture data; the components and message selection are production code.
const conversation = { conversation_id: run.conversation_id, title: "排版回归", project_id: "default",
  project_label: "默认项目", workspace_path: "layout-conversation", created_at:"2026-10-22T00:00:00Z", archived_at: null, runs: [run] };
let settingsDraft: Record<string, unknown> = { default_mode: "auto", default_workflow_id: null, default_agent_ids: [], log_level: "warning",
  hermes_enabled: false, safe_tools_enabled: true, require_approval_for_tools: true, tool_approval_mode: "ask",
  default_permission_mode: "default", default_sandbox_profile: "workspace_write",
  supported_permission_modes: ["default", "auto_review", "read_only"], channel_entry: "main_agent",
  default_execution_backend: "systemd", attachment_retention_days: 7, attachment_max_mb: 25 };
window.fetch = async (input, init) => {
  const url = new URL(String(input), location.origin), path = url.pathname;
  if (init?.method && init.method !== "GET") {
    if (location.pathname === "/config" && path === "/api/v1/admin/settings" && init.method === "PUT") {
      settingsDraft = { ...settingsDraft, ...JSON.parse(String(init.body)) };
      return new Response(JSON.stringify(settingsDraft), { headers: { "Content-Type": "application/json" } });
    }
    throw new Error("Only local fixture settings may be saved");
  }
  const data = path === "/api/v1/auth/me" ? {user_id:"11111111-1111-4111-8111-111111111111",
    tenant_id:"00000000-0000-4000-8000-000000000001",username:"test",role:"super_admin",permissions:["*"]}
    : path === "/api/v1/admin/runs" ? [run]
    : path === `/api/v1/admin/runs/${run.id}` ? run
    : path === "/api/v1/admin/conversations" ? url.searchParams.get("archived") === "true"
      ? [{ ...conversation, conversation_id: "layout-archived", title: "已归档排版回归", archived_at: "2026-10-22T01:00:00Z", runs: [] }] : [conversation]
    : path === "/api/v1/admin/conversation-questions/search" ? { items: [{ run_id: run.id,
      conversation_id: run.conversation_id, project_id: "default", conversation_title: "排版回归",
      question: "检查结果和排版", created_at: run.created_at }], next_cursor: null }
    : path === "/api/v1/admin/conversations/layout-conversation" ? conversation
    : path.endsWith("/files") ? { items: [], bundle_download_url: "/unused" }
    : path.endsWith("/project-workspaces") ? [{ project_id: "default", label: "默认项目", workspace_path: "main" }]
    : path.endsWith("/directories") ? { project_id: "default", platform: "linux", separator: "/",
      configured_root: "/fixture/workspaces", logical_root: "projects/default", directories: ["main"], native_picker_available: false }
    : path.endsWith("/execution-backends") ? [{id:"systemd",name:"本机 systemd 隔离",adapter:"SystemdSkillSandbox",
      description:"本地只读布局夹具中的可用执行器状态",available:true,reason:null,isolation:"严格隔离",cost:"本机资源",supported_sandbox_profiles:["none","read_only","restricted","workspace_write"]}]
    : path === "/api/v1/config/current" ? { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", version: 1, status: "published",
      document: { models: {}, agents: [] }, created_by: "11111111-1111-4111-8111-111111111111", created_at: run.created_at }
    : path.endsWith("/settings") ? settingsDraft
    : path.endsWith("/main-agent") ? { model: null, control_mode: "supervisor", decision_policy: "", hermes_policy: "off", max_review_rounds: 2 }
    : [];
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
};
const messages = conversationMessages([run]);
function FixtureRoot() {
  useVisibleViewport();
  if (location.pathname === "/config") {
    sessionStorage.setItem("agent_hub_access_token", "local-layout-fixture");
    return <TestApp initialPath="/config" />;
  }
  if (location.pathname === "/shell") {
    sessionStorage.setItem("agent_hub_access_token", "local-readonly-layout-fixture");
    return <TestApp initialPath="/?conversation=layout-conversation" />;
  }
  if (location.pathname === "/detail" || location.pathname === "/conversation") {
    sessionStorage.setItem("agent_hub_access_token", "local-layout-fixture");
    return <TestApp initialPath={location.pathname === "/detail" ? `/runs/${run.id}` : "/?conversation=layout-conversation"} />;
  }
  return <main className="chat-stream" style={{ maxWidth: 960, margin: "0 auto" }}>
    {messages.map((message) => <article key={message.id} className={`chat-message ${message.role}`}>
      <span className="eyebrow">{message.role === "user" ? "你" : "Mofang"}</span>
      <h3>{message.title}</h3><MessageBody text={message.body} title={message.title} />
    </article>)}
  </main>;
}
createRoot(document.getElementById("root")!).render(<FixtureRoot />);
