import { z } from "zod";

const PrincipalSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string(),
  role: z.string(),
});

const MeSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string(),
  username: z.string(),
  role: z.string(),
  permissions: z.array(z.string()),
});

export type CurrentUser = z.infer<typeof MeSchema>;

const TokenResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.string(),
  principal: PrincipalSchema,
});

type Principal = z.infer<typeof PrincipalSchema>;

const TOKEN_STORAGE_KEY = "agent_hub_access_token";
const TENANT_STORAGE_KEY = "agent_hub_tenant_id";

const ROLE_PERMISSIONS: Record<string, string[]> = {
  super_admin: ["*"],
  admin: [
    "config:*",
    "agent:*",
    "skill:read",
    "skill:use",
    "skill:write",
    "skill:approve",
    "mcp:read",
    "mcp:use",
    "mcp:write",
    "memory:*",
    "hermes:*",
    "run:*",
    "plugin:read",
    "plugin:use",
    "plugin:write",
    "user:read",
    "user:write",
    "audit:read",
  ],
  operator: [
    "run:create",
    "run:read",
    "run:pause",
    "run:resume",
    "run:cancel",
    "config:read",
    "skill:read",
    "skill:use",
    "mcp:read",
    "mcp:use",
    "plugin:read",
    "plugin:use",
  ],
  viewer: [
    "run:read",
    "config:read",
    "skill:read",
    "skill:use",
    "mcp:read",
    "mcp:use",
    "plugin:read",
    "plugin:use",
  ],
};

function safeSessionGet(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSessionSet(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Ignore storage failures; the in-memory token still works for the current page lifetime.
  }
}

function safeSessionRemove(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Ignore storage failures.
  }
}

let accessToken = safeSessionGet(TOKEN_STORAGE_KEY);

function currentAccessToken(): string | null {
  return accessToken ?? safeSessionGet(TOKEN_STORAGE_KEY);
}

function principalToCurrentUser(principal: Principal): CurrentUser {
  return {
    user_id: principal.user_id,
    tenant_id: principal.tenant_id,
    username: `${principal.role}:${principal.user_id.slice(0, 8)}`,
    role: principal.role,
    permissions: ROLE_PERMISSIONS[principal.role] ?? [],
  };
}

function archiveContentType(filename: string): string {
  const lowered = filename.toLowerCase();
  if (lowered.endsWith(".zip")) return "application/zip";
  if (lowered.endsWith(".tar")) return "application/x-tar";
  if (lowered.endsWith(".tar.gz") || lowered.endsWith(".tgz")) return "application/gzip";
  return "application/octet-stream";
}

function rememberSession(token: string, principal: Principal): CurrentUser {
  accessToken = token;
  safeSessionSet(TOKEN_STORAGE_KEY, token);
  safeSessionSet(TENANT_STORAGE_KEY, principal.tenant_id);
  return principalToCurrentUser(principal);
}

function clearSession(): void {
  accessToken = null;
  safeSessionRemove(TOKEN_STORAGE_KEY);
  safeSessionRemove(TENANT_STORAGE_KEY);
}

export function rememberedTenantId(): string {
  return safeSessionGet(TENANT_STORAGE_KEY) ?? "";
}

const UserSchema = z.object({
  id: z.string(),
  username: z.string(),
  role: z.string(),
  disabled: z.boolean(),
  feishu_open_id: z.string().nullable(),
  protected: z.boolean(),
});

export type ManagedUser = z.infer<typeof UserSchema>;

const ModelDeploymentSchema = z.object({
  id: z.string(),
  provider: z.string(),
  api_base: z.string(),
  api_protocol: z.enum(["openai_compatible", "anthropic_messages"]).default("openai_compatible"),
  upstream_model: z.string(),
  logical_model: z.string(),
  capabilities: z.array(z.string()),
  credential_ref: z.string(),
  quota_scope: z.string(),
  max_concurrency: z.number(),
  target_utilization: z.number(),
  reserved_capacity: z.number(),
  rpm: z.number().nullable(),
  tpm: z.number().nullable(),
  queue_timeout_seconds: z.number(),
  fallback: z.string().nullable(),
  weight: z.number(),
  effective_slots: z.number(),
  saturation_policy: z.string(),
});

export type ModelDeployment = z.infer<typeof ModelDeploymentSchema>;

const SecretReferenceSchema = z.object({
  ref: z.string(),
  last_four: z.string(),
});

export type SecretReference = z.infer<typeof SecretReferenceSchema>;

const DiffSchema = z.object({
  added: z.array(z.string()),
  removed: z.array(z.string()),
  changed: z.array(z.string()),
});

export type ConfigDiff = z.infer<typeof DiffSchema>;

const NamedResourceSchema = z.object({
  id: z.string(),
  name: z.string(),
  enabled: z.boolean(),
  role: z.string().nullable().optional(),
  prompt: z.string().nullable().optional(),
  model: z.string().nullable().optional(),
  skills: z.array(z.string()).optional(),
});

export type NamedResource = z.infer<typeof NamedResourceSchema>;

const WorkflowResourceSchema = NamedResourceSchema.extend({
  mode: z.enum(["auto", "direct", "dispatch", "discuss", "hybrid"]).nullable().optional(),
  task_type: z.string().nullable().optional(),
  allow_main_agent_override: z.boolean().default(false),
  allow_temporary_agents: z.boolean().default(false),
  temporary_agent_policy: z.string().nullable().optional(),
  role_selection_policy: z.string().nullable().optional(),
  agent_ids: z.array(z.string()).optional(),
  objective: z.string().nullable().optional(),
  steps: z.array(z.string()).optional(),
  deliverables: z.array(z.string()).optional(),
  decision_policy: z.string().nullable().optional(),
});

export type WorkflowResource = z.infer<typeof WorkflowResourceSchema>;

const SystemSettingsSchema = z.object({
  default_mode: z.enum(["auto", "direct", "dispatch", "discuss", "hybrid"]),
  default_workflow_id: z.string().nullable(),
  default_agent_ids: z.array(z.string()),
  log_level: z.enum(["warning", "error"]),
  hermes_enabled: z.boolean(),
  safe_tools_enabled: z.boolean(),
  require_approval_for_tools: z.boolean(),
  allow_main_agent_override: z.boolean().default(false),
  allow_temporary_agents: z.boolean().default(false),
  vibe_coding_enabled: z.boolean().default(false),
  multimedia_generation_enabled: z.boolean().default(false),
  openclaw_enabled: z.boolean().default(false),
  openclaw_mode: z.enum(["ask", "read_only", "auto_review", "trusted_auto"]).default("ask"),
  openclaw_allowed_commands: z.array(z.array(z.string())).default([]),
  temporary_agent_policy: z.string().default(
    "主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
  ),
  channel_entry: z.string(),
  attachment_retention_days: z.number(),
  attachment_max_mb: z.number(),
});

export type SystemSettings = z.infer<typeof SystemSettingsSchema>;

const OpenClawOperationRequestSchema = z.object({
  platform: z.enum(["linux", "windows", "macos"]),
  kind: z.enum(["server_command", "desktop_action", "screen_read", "file_read"]),
  target: z.string(),
  argv: z.array(z.string()),
  risk_level: z.enum(["low", "medium", "high"]),
  reason: z.string(),
  session_id: z.string().optional(),
});

export type OpenClawOperationRequest = z.infer<typeof OpenClawOperationRequestSchema>;

const OpenClawOperationSchema = z.object({
  id: z.string(),
  status: z.enum(["waiting_user_approval", "approved", "rejected", "executed"]),
  approval_id: z.string(),
  requires_user_approval: z.boolean(),
  platform: z.string(),
  kind: z.string(),
  operation: z.record(z.string(), z.unknown()),
  approval_summary: z.string(),
  requested_by: z.string(),
  created_at: z.string(),
  resolved_by: z.string().nullable().optional(),
  resolved_at: z.string().nullable().optional(),
  execution: z.record(z.string(), z.unknown()).nullable().optional(),
});

export type OpenClawOperation = z.infer<typeof OpenClawOperationSchema>;

const OpenClawExecutionSchema = z.object({
  operation: OpenClawOperationSchema,
  exit_code: z.number(),
  stdout: z.string(),
  stderr: z.string(),
  truncated: z.boolean(),
});

export type OpenClawExecution = z.infer<typeof OpenClawExecutionSchema>;

const OpenClawAdapterSchema = z.object({
  platform: z.enum(["linux", "windows", "macos"]),
  kind: z.enum(["server_command", "desktop_action", "screen_read", "file_read"]),
  target_type: z.enum(["server", "computer", "desktop", "filesystem", "screen"]),
  status: z.enum(["available", "adapter_unavailable"]),
  execution_host: z.string(),
  requires_user_approval: z.boolean(),
  supports_read_only: z.boolean(),
  description: z.string(),
});

export type OpenClawAdapter = z.infer<typeof OpenClawAdapterSchema>;

const OpenClawSessionRequestSchema = z.object({
  platform: z.enum(["linux", "windows", "macos"]),
  target_type: z.enum(["server", "computer", "desktop"]),
  target: z.string(),
  purpose: z.string(),
});

export type OpenClawSessionRequest = z.infer<typeof OpenClawSessionRequestSchema>;

const OpenClawSessionSchema = z.object({
  id: z.string(),
  status: z.enum(["active", "paused", "stopped", "adapter_unavailable"]),
  adapter_status: z.enum(["available", "adapter_unavailable"]),
  mode: z.enum(["ask", "read_only", "auto_review", "trusted_auto"]),
  platform: z.string(),
  target_type: z.string(),
  target: z.string(),
  purpose: z.string(),
  execution_host: z.string(),
  requested_by: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  stopped_at: z.string().nullable().optional(),
  operation_ids: z.array(z.string()),
});

export type OpenClawSession = z.infer<typeof OpenClawSessionSchema>;

const MainAgentModelConfigSchema = z.object({
  provider: z.string(),
  api_base: z.string(),
  api_protocol: z.enum(["openai_compatible", "anthropic_messages"]),
  upstream_model: z.string(),
  credential_ref: z.string(),
  capabilities: z.array(z.string()),
});

const MainAgentConfigSchema = z.object({
  model: MainAgentModelConfigSchema.nullable(),
  control_mode: z.enum(["supervisor", "planner", "reviewer", "autonomous"]),
  decision_policy: z.string(),
  operating_style: z.string().default("control the room, clarify goals, choose mode and roles, decide conflicts, review failures"),
  direct_answerer: z.string().default("main_agent"),
  hermes_policy: z.enum(["off", "observe", "suggest", "confirm_before_apply"]),
  max_review_rounds: z.number(),
});

export type MainAgentConfig = z.infer<typeof MainAgentConfigSchema>;

const ConfigRevisionSchema = z.object({
  id: z.string(),
  version: z.number(),
  status: z.string(),
  document: z.object({
    models: z.record(z.string(), z.unknown()),
    agents: z.array(z.unknown()),
  }),
  created_by: z.string().nullable(),
  created_at: z.string(),
  notification_status: z.string().optional(),
});

export type ConfigRevision = z.infer<typeof ConfigRevisionSchema>;

const RunListItemSchema = z.object({
  id: z.string(),
  status: z.string(),
  mode: z.string(),
  conversation_id: z.string().nullable().optional(),
  queue_wait_ms: z.number(),
  capacity_wait_ms: z.number(),
  cost_usd: z.string(),
});

export type RunListItem = z.infer<typeof RunListItemSchema>;

const TemporaryAgentProposalSchema = z.object({
  id: z.string(),
  name: z.string(),
  role: z.string(),
  prompt: z.string(),
  reason: z.string(),
  missing_capability: z.string(),
  model: z.string().optional(),
  suggested_skills: z.array(z.string()),
  permanentizable: z.boolean(),
});

const ScheduleProposalSchema = z.object({
  name: z.string(),
  message: z.string(),
  mode: z.enum(["auto", "direct", "dispatch", "discuss", "hybrid"]),
  workflow_id: z.string(),
  kind: z.enum(["one_time", "cron"]),
  timezone: z.string(),
  misfire_policy: z.enum(["fire_once", "skip"]),
  budget: z.number(),
  run_at: z.string().nullable().optional(),
  cron: z.string().nullable().optional(),
  summary: z.string(),
  metadata: z.record(z.string(), z.string()),
});

const SubmittedRunSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  status: z.string(),
  mode: z.string().nullable(),
  decision_token: z.string().nullable(),
  version: z.number(),
  clarification_reason: z.string().nullable(),
  conversation_id: z.string().nullable().optional(),
  reference_conversation_id: z.string().nullable().optional(),
  temporary_agent_proposal: TemporaryAgentProposalSchema.nullable().optional(),
  schedule_proposal: ScheduleProposalSchema.nullable().optional(),
});

export type SubmittedRun = z.infer<typeof SubmittedRunSchema>;

const RunEventSchema = z.object({
  sequence: z.number(),
  kind: z.string(),
  message: z.string(),
  created_at: z.string(),
  actor: z.string().nullable().optional(),
  participants: z.array(z.string()).default([]),
  tool_name: z.string().nullable().optional(),
  step_id: z.string().nullable().optional(),
  action: z.string().nullable().optional(),
  decision: z.string().nullable().optional(),
  payload: z.record(z.string(), z.unknown()).default({}),
  artifact: z
    .object({
      id: z.string(),
      kind: z.string(),
      title: z.string(),
      text: z.string().nullable().optional(),
    })
    .nullable()
    .optional(),
});

const RunArtifactSchema = z.object({
  id: z.string(),
  kind: z.string(),
  title: z.string(),
  text: z.string().nullable().optional(),
});

const RunDetailSchema = RunListItemSchema.extend({
  request: z.string(),
  events: z.array(RunEventSchema),
  artifacts: z.array(RunArtifactSchema),
  explicit_details: z.record(z.string(), z.string()),
  decision_token: z.string().nullable().optional(),
  temporary_agent_proposal: TemporaryAgentProposalSchema.nullable().optional(),
  schedule_proposal: ScheduleProposalSchema.nullable().optional(),
});

const RunDeleteSchema = z.object({
  id: z.string(),
  deleted: z.boolean(),
});

const BulkFailureSchema = z.object({
  id: z.string(),
  code: z.string(),
  message: z.string(),
});

const RunBulkDeleteSchema = z.object({
  deleted: z.array(RunDeleteSchema),
  failed: z.array(BulkFailureSchema),
});

export type RunDetail = z.infer<typeof RunDetailSchema>;
export type RunDeleteResult = z.infer<typeof RunDeleteSchema>;
export type BulkFailure = z.infer<typeof BulkFailureSchema>;
export type RunBulkDeleteResult = z.infer<typeof RunBulkDeleteSchema>;

const ConversationSchema = z.object({
  conversation_id: z.string(),
  runs: z.array(RunDetailSchema),
});

export type Conversation = z.infer<typeof ConversationSchema>;

const SkillSchema = z.object({
  id: z.string(),
  name: z.string(),
  status: z.string(),
  scan_diff: z.array(z.string()),
  requested_permissions: z.array(z.string()),
});

export type Skill = z.infer<typeof SkillSchema>;

const SkillArchiveUploadSchema = z.object({
  filename: z.string(),
  bundle: z.boolean(),
  items: z.array(SkillSchema),
});

export type SkillArchiveUpload = z.infer<typeof SkillArchiveUploadSchema>;

const AttachmentUploadSchema = z.object({
  id: z.string(),
  filename: z.string(),
  kind: z.string(),
  content_type: z.string(),
  size_bytes: z.number(),
  sha256: z.string(),
  expires_at: z.string(),
});

export type AttachmentUpload = z.infer<typeof AttachmentUploadSchema>;

const AttachmentListSchema = z.object({
  items: z.array(AttachmentUploadSchema),
});

const AttachmentDeleteSchema = z.object({
  id: z.string(),
  deleted: z.boolean(),
});

const AttachmentBulkDeleteSchema = z.object({
  deleted: z.array(z.string()),
  failed: z.array(BulkFailureSchema),
});

const MultimediaGenerationSchema = z.object({
  kind: z.enum(["image", "video", "audio"]),
  logical_model: z.string(),
  deployment_id: z.string(),
  text: z.string().nullable(),
});

export type MultimediaGeneration = z.infer<typeof MultimediaGenerationSchema>;

const ScheduleSchema = z.object({
  id: z.string(),
  name: z.string(),
  status: z.string(),
  kind: z.enum(["one_time", "cron"]),
  mode: z.enum(["auto", "direct", "dispatch", "discuss", "hybrid"]),
  workflow_id: z.string(),
  message: z.string(),
  timezone: z.string(),
  next_fire_at: z.string().nullable(),
  run_at: z.string().nullable(),
  cron: z.string().nullable(),
  misfire_policy: z.enum(["fire_once", "skip"]),
  budget: z.number(),
  metadata: z.record(z.string(), z.string()),
});

export type Schedule = z.infer<typeof ScheduleSchema>;

export type ScheduleCreatePayload = {
  name: string;
  message: string;
  mode: "auto" | "direct" | "dispatch" | "discuss" | "hybrid";
  workflow_id: string;
  kind: "one_time" | "cron";
  run_at?: string | null;
  cron?: string | null;
  timezone: string;
  misfire_policy: "fire_once" | "skip";
  budget: number;
  metadata: Record<string, string>;
};

const McpServerSchema = z.object({
  id: z.string(),
  name: z.string(),
  health: z.string(),
  allowed_tools: z.array(z.string()),
  transport: z.string().default("streamable_http"),
  command: z.string().nullable().default(null),
  args: z.array(z.string()).default([]),
  url: z.string().nullable().default(null),
  executable_allowlist: z.array(z.string()).default([]),
  domain_allowlist: z.array(z.string()).default([]),
  timeout_seconds: z.number().default(10),
});

export type McpServer = z.infer<typeof McpServerSchema>;

const ChannelStatusSchema = z.object({
  id: z.string(),
  name: z.string(),
  status: z.string(),
  transports: z.array(z.string()),
  webhook_path: z.string().nullable(),
  public_webhook_url: z.string().nullable(),
  missing: z.array(z.string()),
  notes: z.array(z.string()),
});

export type ChannelStatus = z.infer<typeof ChannelStatusSchema>;

const ChannelConfigSaveSchema = z.object({
  id: z.string(),
  saved: z.array(z.string()),
  status: ChannelStatusSchema,
});

export type ChannelConfigSave = z.infer<typeof ChannelConfigSaveSchema>;

const MemoryRecordSchema = z.object({
  id: z.string(),
  scope: z.string(),
  value: z.string(),
});

export type MemoryRecord = z.infer<typeof MemoryRecordSchema>;

const AuditEventSchema = z.object({
  id: z.string(),
  actor: z.string(),
  action: z.string(),
  resource: z.string(),
  created_at: z.string(),
});

export type AuditEvent = z.infer<typeof AuditEventSchema>;

const LogEntrySchema = z.object({
  id: z.string(),
  category: z.string(),
  level: z.string(),
  title: z.string(),
  message: z.string(),
  source: z.string(),
  details: z.record(z.string(), z.string()),
  created_at: z.string(),
});

export type LogEntry = z.infer<typeof LogEntrySchema>;

const HermesInsightSchema = z.object({
  id: z.string(),
  outcome: z.string(),
  lesson: z.string(),
  summary: z.string(),
  run_id: z.string().nullable(),
  conversation_id: z.string().nullable(),
  confirmed_at: z.string().nullable(),
  tags: z.array(z.string()),
  weight: z.number(),
  created_at: z.string(),
});

export type HermesInsight = z.infer<typeof HermesInsightSchema>;

const HermesBulkConfirmSchema = z.object({
  confirmed: z.array(HermesInsightSchema),
  failed: z.array(BulkFailureSchema),
});

export type HermesBulkConfirmResult = z.infer<typeof HermesBulkConfirmSchema>;

const HermesBulkDeleteSchema = z.object({
  deleted: z.array(z.string()),
  failed: z.array(BulkFailureSchema),
});

export type HermesBulkDeleteResult = z.infer<typeof HermesBulkDeleteSchema>;

const OperationStatusSchema = z.object({
  status: z.string(),
});

export type OperationStatus = z.infer<typeof OperationStatusSchema>;

const HermesRecommendationSchema = z.object({
  recommended_mode: z.string(),
  recommended_model: z.string().nullable(),
  recommended_skills: z.array(z.string()),
  confidence: z.number(),
  reasons: z.array(z.string()),
  requires_approval: z.boolean(),
});

export type HermesRecommendation = z.infer<typeof HermesRecommendationSchema>;

const ErrorDetailValueSchema = z.union([z.string(), z.number(), z.boolean(), z.null()]);

const ErrorEnvelopeSchema = z.object({
  error: z.union([
    z.string(),
    z.object({
      code: z.string(),
      message: z.string(),
      details: z.record(z.string(), ErrorDetailValueSchema).optional(),
    }),
  ]),
});

export type ApiErrorDetails = Record<string, string | number | boolean | null>;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code: string = "request_failed",
    public readonly errorId: string | null = null,
    public readonly details: ApiErrorDetails | null = null,
  ) {
    super(message);
  }
}

export function formatApiError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  const parts = [error.code, `HTTP ${error.status}`];
  if (error.errorId) parts.push(`error ${error.errorId}`);
  return `${fallback}: ${error.message} (${parts.join(", ")})`;
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const errorId = response.headers.get("x-error-id");
  const fallbackMessage = response.statusText || "request failed";
  try {
    const payload: unknown = await response.json();
    const parsed = ErrorEnvelopeSchema.safeParse(payload);
    if (parsed.success) {
      const error = parsed.data.error;
      if (typeof error === "string") {
        return new ApiError(error || fallbackMessage, response.status, "request_failed", errorId);
      }
      return new ApiError(error.message, response.status, error.code, errorId, error.details ?? null);
    }
  } catch {
    return new ApiError(fallbackMessage, response.status, "invalid_error_response", errorId);
  }
  return new ApiError(fallbackMessage, response.status, "invalid_error_response", errorId);
}

async function request<T>(
  path: string,
  init: RequestInit,
  schema: z.ZodType<T>,
): Promise<T> {
  let response: Response;
  const token = currentAccessToken();
  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError("network request failed", 0, "network_error");
  }
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  const payload = await response.json();
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError(
      `response schema validation failed for ${path}`,
      response.status,
      "invalid_response",
    );
  }
  return parsed.data;
}

async function requestNoContent(path: string, init: RequestInit): Promise<void> {
  let response: Response;
  const token = currentAccessToken();
  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError("network request failed", 0, "network_error");
  }
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
}

async function requestBinary<T>(
  path: string,
  init: RequestInit,
  schema: z.ZodType<T>,
): Promise<T> {
  let response: Response;
  const token = currentAccessToken();
  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError("network request failed", 0, "network_error");
  }
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  const payload = await response.json();
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError(
      `response schema validation failed for ${path}`,
      response.status,
      "invalid_response",
    );
  }
  return parsed.data;
}

export const api = {
  me(): Promise<CurrentUser> {
    return request("/api/v1/auth/me", { method: "GET" }, PrincipalSchema).then((principal) =>
      principalToCurrentUser(principal),
    );
  },
  login(username: string, password: string, tenant_id = ""): Promise<CurrentUser> {
    const trimmedTenantId = tenant_id.trim();
    return request(
      "/api/v1/auth/login",
      {
        method: "POST",
        body: JSON.stringify({
          ...(trimmedTenantId ? { tenant_id: trimmedTenantId } : {}),
          username,
          password,
        }),
      },
      TokenResponseSchema,
    ).then((response) => rememberSession(response.access_token, response.principal));
  },
  setup(code: string, username: string, password: string): Promise<CurrentUser> {
    return request(
      "/api/v1/setup",
      { method: "POST", body: JSON.stringify({ code, username, password }) },
      TokenResponseSchema,
    ).then((response) => rememberSession(response.access_token, response.principal));
  },
  async logout(): Promise<void> {
    clearSession();
  },
  users(): Promise<ManagedUser[]> {
    return request("/api/v1/users", { method: "GET" }, z.array(UserSchema));
  },
  changeUserRole(userId: string, role: string): Promise<ManagedUser> {
    return request(
      `/api/v1/users/${userId}/role`,
      { method: "PATCH", body: JSON.stringify({ role }) },
      UserSchema,
    );
  },
  createUser(payload: { username: string; password: string; role: string }): Promise<ManagedUser> {
    return request(
      "/api/v1/users",
      { method: "POST", body: JSON.stringify(payload) },
      UserSchema,
    );
  },
  updateUser(
    userId: string,
    payload: { username?: string; role?: string; disabled?: boolean },
  ): Promise<ManagedUser> {
    return request(
      `/api/v1/users/${userId}`,
      { method: "PATCH", body: JSON.stringify(payload) },
      UserSchema,
    );
  },
  setUserDisabled(userId: string, disabled: boolean): Promise<ManagedUser> {
    return request(
      `/api/v1/users/${userId}/disabled`,
      { method: "PATCH", body: JSON.stringify({ disabled }) },
      UserSchema,
    );
  },
  resetUserPassword(userId: string, password: string): Promise<ManagedUser> {
    return request(
      `/api/v1/users/${userId}/password`,
      { method: "PATCH", body: JSON.stringify({ password }) },
      UserSchema,
    );
  },
  async deleteUser(userId: string): Promise<void> {
    await requestNoContent(`/api/v1/users/${userId}`, { method: "DELETE" });
  },
  models(): Promise<ModelDeployment[]> {
    return request("/api/v1/admin/models", { method: "GET" }, z.array(ModelDeploymentSchema));
  },
  createModel(payload: Omit<ModelDeployment, "id" | "effective_slots" | "saturation_policy">) {
    return request(
      "/api/v1/admin/models",
      { method: "POST", body: JSON.stringify(payload) },
      ModelDeploymentSchema,
    );
  },
  updateModel(id: string, payload: Omit<ModelDeployment, "id" | "effective_slots" | "saturation_policy">) {
    return request(
      `/api/v1/admin/models/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify(payload) },
      ModelDeploymentSchema,
    );
  },
  async deleteModel(id: string): Promise<void> {
    await requestNoContent(`/api/v1/admin/models/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
  createSecret(label: string, value: string): Promise<SecretReference> {
    return request(
      "/api/v1/admin/secrets",
      { method: "POST", body: JSON.stringify({ label, value }) },
      SecretReferenceSchema,
    );
  },
  probeModel(quota_scope: string, desired_concurrency: number) {
    return request(
      "/api/v1/admin/models/probe",
      { method: "POST", body: JSON.stringify({ quota_scope, desired_concurrency }) },
      z.object({ recommended_concurrency: z.number(), warning: z.string() }),
    );
  },
  diffConfig(yaml: string): Promise<ConfigDiff> {
    return request(
      "/api/v1/admin/config/diff",
      { method: "POST", body: JSON.stringify({ yaml }) },
      DiffSchema,
    );
  },
  publishConfig(expected_version: number) {
    return request(
      "/api/v1/admin/config/publish",
      { method: "POST", body: JSON.stringify({ expected_version }) },
      z.object({ version: z.number(), status: z.string() }),
    );
  },
  currentConfig(): Promise<ConfigRevision> {
    return request("/api/v1/config/current", { method: "GET" }, ConfigRevisionSchema);
  },
  createConfigDraft(document: { agents: unknown[]; models: Record<string, unknown> }) {
    return request(
      "/api/v1/config/drafts",
      { method: "POST", body: JSON.stringify(document) },
      ConfigRevisionSchema,
    );
  },
  publishConfigDraft(revisionId: string) {
    return request(
      `/api/v1/config/drafts/${revisionId}/publish`,
      { method: "POST" },
      ConfigRevisionSchema,
    );
  },
  agents(): Promise<NamedResource[]> {
    return request("/api/v1/admin/agents", { method: "GET" }, z.array(NamedResourceSchema));
  },
  createAgent(payload: NamedResource): Promise<NamedResource> {
    return request(
      "/api/v1/admin/agents",
      { method: "POST", body: JSON.stringify(payload) },
      NamedResourceSchema,
    );
  },
  deleteAgent(id: string): Promise<{ status: string }> {
    return request(
      `/api/v1/admin/agents/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      z.object({ status: z.string() }),
    );
  },
  workflows(): Promise<WorkflowResource[]> {
    return request(
      "/api/v1/admin/workflows",
      { method: "GET" },
      z.array(WorkflowResourceSchema),
    );
  },
  createWorkflow(payload: WorkflowResource): Promise<WorkflowResource> {
    return request(
      "/api/v1/admin/workflows",
      { method: "POST", body: JSON.stringify(payload) },
      WorkflowResourceSchema,
    );
  },
  deleteWorkflow(id: string): Promise<{ status: string }> {
    return request(
      `/api/v1/admin/workflows/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      z.object({ status: z.string() }),
    );
  },
  settings(): Promise<SystemSettings> {
    return request("/api/v1/admin/settings", { method: "GET" }, SystemSettingsSchema);
  },
  updateSettings(payload: SystemSettings): Promise<SystemSettings> {
    return request(
      "/api/v1/admin/settings",
      { method: "PUT", body: JSON.stringify(payload) },
      SystemSettingsSchema,
    );
  },
  createOpenClawOperation(payload: OpenClawOperationRequest): Promise<OpenClawOperation> {
    return request(
      "/api/v1/admin/openclaw/operations",
      { method: "POST", body: JSON.stringify(payload) },
      OpenClawOperationSchema,
    );
  },
  resolveOpenClawOperation(id: string, decision: "approve" | "reject"): Promise<OpenClawOperation> {
    return request(
      `/api/v1/admin/openclaw/operations/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify({ decision }) },
      OpenClawOperationSchema,
    );
  },
  executeOpenClawOperation(id: string): Promise<OpenClawExecution> {
    return request(
      `/api/v1/admin/openclaw/operations/${encodeURIComponent(id)}/execute`,
      { method: "POST" },
      OpenClawExecutionSchema,
    );
  },
  openClawAdapters(): Promise<OpenClawAdapter[]> {
    return request(
      "/api/v1/admin/openclaw/adapters",
      { method: "GET" },
      z.array(OpenClawAdapterSchema),
    );
  },
  createOpenClawSession(payload: OpenClawSessionRequest): Promise<OpenClawSession> {
    return request(
      "/api/v1/admin/openclaw/sessions",
      { method: "POST", body: JSON.stringify(payload) },
      OpenClawSessionSchema,
    );
  },
  openClawSessions(): Promise<OpenClawSession[]> {
    return request(
      "/api/v1/admin/openclaw/sessions",
      { method: "GET" },
      z.array(OpenClawSessionSchema),
    );
  },
  updateOpenClawSession(id: string, action: "pause" | "resume" | "stop"): Promise<OpenClawSession> {
    return request(
      `/api/v1/admin/openclaw/sessions/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify({ action }) },
      OpenClawSessionSchema,
    );
  },
  mainAgent(): Promise<MainAgentConfig> {
    return request("/api/v1/admin/main-agent", { method: "GET" }, MainAgentConfigSchema);
  },
  updateMainAgent(
    payload: MainAgentConfig,
  ): Promise<MainAgentConfig> {
    return request(
      "/api/v1/admin/main-agent",
      { method: "PUT", body: JSON.stringify(payload) },
      MainAgentConfigSchema,
    );
  },
  createRun(payload: {
    message: string;
    mode: "auto" | "direct" | "dispatch" | "discuss" | "hybrid";
    agent_ids?: string[];
    direct_model?: string | null;
    workflow_id?: string | null;
    allow_workflow_adjustment?: boolean;
    conversation_id?: string | null;
    reference_conversation_id?: string | null;
    attachment_ids?: string[];
    vibe_coding?: boolean;
  }): Promise<SubmittedRun> {
    return request(
      "/api/v1/runs",
      { method: "POST", body: JSON.stringify(payload) },
      SubmittedRunSchema,
    );
  },
  uploadAttachment(file: File): Promise<AttachmentUpload> {
    return requestBinary(
      "/api/v1/runs/attachments/upload",
      {
        method: "POST",
        body: file,
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-Agent-Hub-Filename": file.name,
        },
      },
      AttachmentUploadSchema,
    );
  },
  attachments(): Promise<AttachmentUpload[]> {
    return request("/api/v1/runs/attachments", { method: "GET" }, AttachmentListSchema).then((result) => result.items);
  },
  deleteAttachment(id: string): Promise<{ id: string; deleted: boolean }> {
    return request(
      `/api/v1/runs/attachments/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      AttachmentDeleteSchema,
    );
  },
  bulkDeleteAttachments(ids: string[]): Promise<{ deleted: string[]; failed: { id: string; code: string; message: string }[] }> {
    return request(
      "/api/v1/runs/attachments/bulk-delete",
      { method: "POST", body: JSON.stringify({ ids }) },
      AttachmentBulkDeleteSchema,
    );
  },
  generateMultimedia(payload: {
    kind: "image" | "video" | "audio";
    logical_model: string;
    prompt: string;
  }): Promise<MultimediaGeneration> {
    return request(
      "/api/v1/admin/multimedia/generate",
      { method: "POST", body: JSON.stringify(payload) },
      MultimediaGenerationSchema,
    );
  },
  schedules(): Promise<Schedule[]> {
    return request("/api/v1/admin/schedules", { method: "GET" }, z.array(ScheduleSchema));
  },
  createSchedule(payload: ScheduleCreatePayload): Promise<Schedule> {
    return request(
      "/api/v1/admin/schedules",
      { method: "POST", body: JSON.stringify(payload) },
      ScheduleSchema,
    );
  },
  tickSchedules(now: string): Promise<{ fired: string[] }> {
    return request(
      "/api/v1/admin/schedules/tick",
      { method: "POST", body: JSON.stringify({ now }) },
      z.object({ fired: z.array(z.string()) }),
    );
  },
  deleteSchedule(id: string): Promise<{ id: string; deleted: boolean }> {
    return request(
      `/api/v1/admin/schedules/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      z.object({ id: z.string(), deleted: z.boolean() }),
    );
  },
  chooseMode(
    id: string,
    payload: {
      mode: "direct" | "dispatch" | "discuss" | "hybrid";
      decision_token: string;
      version: number;
      operator_note?: string;
    },
  ): Promise<SubmittedRun> {
    return request(
      `/api/v1/runs/${encodeURIComponent(id)}/choose-mode`,
      { method: "POST", body: JSON.stringify(payload) },
      SubmittedRunSchema,
    );
  },
  approveTemporaryAgent(
    id: string,
    payload: { decision_token: string; version: number },
  ): Promise<SubmittedRun> {
    return request(
      `/api/v1/runs/${encodeURIComponent(id)}/approve-temporary-agent`,
      { method: "POST", body: JSON.stringify(payload) },
      SubmittedRunSchema,
    );
  },
  reviseTemporaryAgent(
    id: string,
    payload: { decision_token: string; version: number; feedback: string },
  ): Promise<SubmittedRun> {
    return request(
      `/api/v1/runs/${encodeURIComponent(id)}/revise-temporary-agent`,
      { method: "POST", body: JSON.stringify(payload) },
      SubmittedRunSchema,
    );
  },
  runs(): Promise<RunListItem[]> {
    return request("/api/v1/admin/runs", { method: "GET" }, z.array(RunListItemSchema));
  },
  run(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}`, { method: "GET" }, RunDetailSchema);
  },
  conversation(conversationId: string): Promise<Conversation> {
    return request(
      `/api/v1/admin/conversations/${encodeURIComponent(conversationId)}`,
      { method: "GET" },
      ConversationSchema,
    );
  },
  pauseRun(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}/pause`, { method: "POST" }, RunDetailSchema);
  },
  resumeRun(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}/resume`, { method: "POST" }, RunDetailSchema);
  },
  cancelRun(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}/cancel`, { method: "POST" }, RunDetailSchema);
  },
  deleteRun(id: string): Promise<RunDeleteResult> {
    return request(`/api/v1/admin/runs/${id}`, { method: "DELETE" }, RunDeleteSchema);
  },
  bulkDeleteRuns(ids: string[]): Promise<RunBulkDeleteResult> {
    return request(
      "/api/v1/admin/runs/bulk-delete",
      { method: "POST", body: JSON.stringify({ ids }) },
      RunBulkDeleteSchema,
    );
  },
  skills(): Promise<Skill[]> {
    return request("/api/v1/admin/skills", { method: "GET" }, z.array(SkillSchema));
  },
  uploadSkill(filename: string): Promise<Skill> {
    return request(
      "/api/v1/admin/skills",
      { method: "POST", body: JSON.stringify({ filename }) },
      SkillSchema,
    );
  },
  uploadSkillArchive(file: File): Promise<SkillArchiveUpload> {
    return requestBinary(
      "/api/v1/admin/skills/upload",
      {
        method: "POST",
        body: file,
        headers: {
          "Content-Type": archiveContentType(file.name),
          "X-Agent-Hub-Skill-Filename": file.name,
        },
      },
      SkillArchiveUploadSchema,
    );
  },
  approveSkill(id: string): Promise<Skill> {
    return request(`/api/v1/admin/skills/${id}/approve`, { method: "POST" }, SkillSchema);
  },
  deleteSkill(id: string): Promise<{ status: string }> {
    return request(
      `/api/v1/admin/skills/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      z.object({ status: z.string() }),
    );
  },
  mcpServers(): Promise<McpServer[]> {
    return request("/api/v1/admin/mcp", { method: "GET" }, z.array(McpServerSchema));
  },
  createMcpServer(payload: {
    id: string;
    name: string;
    allowed_tools: string[];
    transport: string;
    command?: string | null;
    args?: string[];
    url?: string | null;
    executable_allowlist?: string[];
    domain_allowlist?: string[];
    timeout_seconds?: number;
  }): Promise<McpServer> {
    return request(
      "/api/v1/admin/mcp",
      { method: "POST", body: JSON.stringify(payload) },
      McpServerSchema,
    );
  },
  deleteMcpServer(id: string): Promise<{ status: string }> {
    return request(
      `/api/v1/admin/mcp/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      z.object({ status: z.string() }),
    );
  },
  channels(): Promise<ChannelStatus[]> {
    return request("/api/v1/admin/channels", { method: "GET" }, z.array(ChannelStatusSchema));
  },
  saveChannelConfig(id: string, payload: { values: Record<string, string> }): Promise<ChannelConfigSave> {
    return request(
      `/api/v1/admin/channels/${encodeURIComponent(id)}/config`,
      { method: "POST", body: JSON.stringify(payload) },
      ChannelConfigSaveSchema,
    );
  },
  memory(): Promise<MemoryRecord[]> {
    return request("/api/v1/admin/memory", { method: "GET" }, z.array(MemoryRecordSchema));
  },
  createMemory(payload: { id: string; scope: string; value: string }): Promise<MemoryRecord> {
    return request(
      "/api/v1/admin/memory",
      { method: "POST", body: JSON.stringify(payload) },
      MemoryRecordSchema,
    );
  },
  updateMemory(id: string, value: string): Promise<MemoryRecord> {
    return request(
      `/api/v1/admin/memory/${id}`,
      { method: "PATCH", body: JSON.stringify({ value }) },
      MemoryRecordSchema,
    );
  },
  async forgetMemory(id: string): Promise<void> {
    await requestNoContent(`/api/v1/admin/memory/${id}`, { method: "DELETE" });
  },
  audit(action?: string): Promise<AuditEvent[]> {
    const query = action ? `?action=${encodeURIComponent(action)}` : "";
    return request(`/api/v1/admin/audit${query}`, { method: "GET" }, z.array(AuditEventSchema));
  },
  logs(category?: string): Promise<LogEntry[]> {
    const query = category ? `?category=${encodeURIComponent(category)}` : "";
    return request(`/api/v1/admin/logs${query}`, { method: "GET" }, z.array(LogEntrySchema));
  },
  hermesInsights(): Promise<HermesInsight[]> {
    return request("/api/v1/admin/hermes", { method: "GET" }, z.array(HermesInsightSchema));
  },
  hermesInsight(id: string): Promise<HermesInsight> {
    return request(`/api/v1/admin/hermes/${encodeURIComponent(id)}`, { method: "GET" }, HermesInsightSchema);
  },
  confirmHermesInsight(id: string): Promise<HermesInsight> {
    return request(`/api/v1/admin/hermes/${encodeURIComponent(id)}/confirm`, { method: "POST" }, HermesInsightSchema);
  },
  deleteHermesInsight(id: string): Promise<OperationStatus> {
    return request(`/api/v1/admin/hermes/${encodeURIComponent(id)}`, { method: "DELETE" }, OperationStatusSchema);
  },
  bulkConfirmHermesInsights(ids: string[]): Promise<HermesBulkConfirmResult> {
    return request(
      "/api/v1/admin/hermes/bulk-confirm",
      { method: "POST", body: JSON.stringify({ ids }) },
      HermesBulkConfirmSchema,
    );
  },
  bulkDeleteHermesInsights(ids: string[]): Promise<HermesBulkDeleteResult> {
    return request(
      "/api/v1/admin/hermes/bulk-delete",
      { method: "POST", body: JSON.stringify({ ids }) },
      HermesBulkDeleteSchema,
    );
  },
  recordHermesFeedback(payload: {
    run_id?: string | null;
    conversation_id?: string | null;
    outcome: "success" | "failure" | "neutral";
    lesson: string;
    tags: string[];
    weight: number;
  }): Promise<HermesInsight> {
    return request(
      "/api/v1/admin/hermes/feedback",
      { method: "POST", body: JSON.stringify(payload) },
      HermesInsightSchema,
    );
  },
  recommendWithHermes(payload: {
    task: string;
    mode_candidates: string[];
    model_candidates: string[];
    skill_candidates: string[];
  }): Promise<HermesRecommendation> {
    return request(
      "/api/v1/admin/hermes/recommend",
      { method: "POST", body: JSON.stringify(payload) },
      HermesRecommendationSchema,
    );
  },
};
