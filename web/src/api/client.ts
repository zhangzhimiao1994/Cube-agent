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
    "skill:*",
    "mcp:*",
    "memory:*",
    "hermes:*",
    "run:*",
    "audit:read",
  ],
  operator: ["run:create", "run:read", "run:pause", "run:resume", "run:cancel", "config:read"],
  viewer: ["run:read", "config:read", "audit:read"],
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
});

export type ManagedUser = z.infer<typeof UserSchema>;

const ModelDeploymentSchema = z.object({
  id: z.string(),
  provider: z.string(),
  api_base: z.string(),
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
  queue_wait_ms: z.number(),
  capacity_wait_ms: z.number(),
  cost_usd: z.string(),
});

export type RunListItem = z.infer<typeof RunListItemSchema>;

const SubmittedRunSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  status: z.string(),
  mode: z.string().nullable(),
  decision_token: z.string().nullable(),
  version: z.number(),
  clarification_reason: z.string().nullable(),
});

export type SubmittedRun = z.infer<typeof SubmittedRunSchema>;

const RunEventSchema = z.object({
  sequence: z.number(),
  kind: z.string(),
  message: z.string(),
  created_at: z.string(),
});

const RunArtifactSchema = z.object({
  id: z.string(),
  kind: z.string(),
  title: z.string(),
});

const RunDetailSchema = RunListItemSchema.extend({
  request: z.string(),
  events: z.array(RunEventSchema),
  artifacts: z.array(RunArtifactSchema),
  explicit_details: z.record(z.string(), z.string()),
});

export type RunDetail = z.infer<typeof RunDetailSchema>;

const SkillSchema = z.object({
  id: z.string(),
  name: z.string(),
  status: z.string(),
  scan_diff: z.array(z.string()),
  requested_permissions: z.array(z.string()),
});

export type Skill = z.infer<typeof SkillSchema>;

const McpServerSchema = z.object({
  id: z.string(),
  name: z.string(),
  health: z.string(),
  allowed_tools: z.array(z.string()),
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
  tags: z.array(z.string()),
  weight: z.number(),
  created_at: z.string(),
});

export type HermesInsight = z.infer<typeof HermesInsightSchema>;

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
  workflows(): Promise<NamedResource[]> {
    return request(
      "/api/v1/admin/workflows",
      { method: "GET" },
      z.array(NamedResourceSchema),
    );
  },
  createWorkflow(payload: NamedResource): Promise<NamedResource> {
    return request(
      "/api/v1/admin/workflows",
      { method: "POST", body: JSON.stringify(payload) },
      NamedResourceSchema,
    );
  },
  createRun(payload: { message: string; mode: "auto" | "direct" | "dispatch" | "discuss" | "hybrid" }): Promise<SubmittedRun> {
    return request(
      "/api/v1/runs",
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
  pauseRun(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}/pause`, { method: "POST" }, RunDetailSchema);
  },
  resumeRun(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}/resume`, { method: "POST" }, RunDetailSchema);
  },
  cancelRun(id: string): Promise<RunDetail> {
    return request(`/api/v1/admin/runs/${id}/cancel`, { method: "POST" }, RunDetailSchema);
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
  uploadSkillArchive(file: File): Promise<Skill> {
    return requestBinary(
      "/api/v1/admin/skills/upload",
      {
        method: "POST",
        body: file,
        headers: {
          "Content-Type": "application/zip",
          "X-Agent-Hub-Skill-Filename": file.name,
        },
      },
      SkillSchema,
    );
  },
  approveSkill(id: string): Promise<Skill> {
    return request(`/api/v1/admin/skills/${id}/approve`, { method: "POST" }, SkillSchema);
  },
  mcpServers(): Promise<McpServer[]> {
    return request("/api/v1/admin/mcp", { method: "GET" }, z.array(McpServerSchema));
  },
  createMcpServer(payload: { id: string; name: string; allowed_tools: string[] }): Promise<McpServer> {
    return request(
      "/api/v1/admin/mcp",
      { method: "POST", body: JSON.stringify(payload) },
      McpServerSchema,
    );
  },
  channels(): Promise<ChannelStatus[]> {
    return request("/api/v1/admin/channels", { method: "GET" }, z.array(ChannelStatusSchema));
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
  recordHermesFeedback(payload: {
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
