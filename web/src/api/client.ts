import { z } from "zod";

const MeSchema = z.object({
  username: z.string(),
  role: z.string(),
  permissions: z.array(z.string()),
});

export type CurrentUser = z.infer<typeof MeSchema>;

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
});

export type NamedResource = z.infer<typeof NamedResourceSchema>;

const RunListItemSchema = z.object({
  id: z.string(),
  status: z.string(),
  mode: z.string(),
  queue_wait_ms: z.number(),
  capacity_wait_ms: z.number(),
  cost_usd: z.string(),
});

export type RunListItem = z.infer<typeof RunListItemSchema>;

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

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init: RequestInit,
  schema: z.ZodType<T>,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    throw new ApiError("request failed", response.status);
  }
  const payload = await response.json();
  return schema.parse(payload);
}

export const api = {
  me(): Promise<CurrentUser> {
    return request("/api/v1/me", { method: "GET" }, MeSchema);
  },
  login(username: string, password: string): Promise<CurrentUser> {
    return request(
      "/api/v1/auth/login",
      { method: "POST", body: JSON.stringify({ username, password }) },
      MeSchema,
    );
  },
  setup(code: string, username: string, password: string): Promise<CurrentUser> {
    return request(
      "/api/v1/setup",
      { method: "POST", body: JSON.stringify({ code, username, password }) },
      MeSchema,
    );
  },
  async logout(): Promise<void> {
    await fetch("/api/v1/auth/logout", {
      method: "POST",
      credentials: "include",
    });
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
  agents(): Promise<NamedResource[]> {
    return request("/api/v1/admin/agents", { method: "GET" }, z.array(NamedResourceSchema));
  },
  workflows(): Promise<NamedResource[]> {
    return request(
      "/api/v1/admin/workflows",
      { method: "GET" },
      z.array(NamedResourceSchema),
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
  approveSkill(id: string): Promise<Skill> {
    return request(`/api/v1/admin/skills/${id}/approve`, { method: "POST" }, SkillSchema);
  },
  mcpServers(): Promise<McpServer[]> {
    return request("/api/v1/admin/mcp", { method: "GET" }, z.array(McpServerSchema));
  },
  memory(): Promise<MemoryRecord[]> {
    return request("/api/v1/admin/memory", { method: "GET" }, z.array(MemoryRecordSchema));
  },
  updateMemory(id: string, value: string): Promise<MemoryRecord> {
    return request(
      `/api/v1/admin/memory/${id}`,
      { method: "PATCH", body: JSON.stringify({ value }) },
      MemoryRecordSchema,
    );
  },
  async forgetMemory(id: string): Promise<void> {
    await fetch(`/api/v1/admin/memory/${id}`, {
      method: "DELETE",
      credentials: "include",
    });
  },
  audit(action?: string): Promise<AuditEvent[]> {
    const query = action ? `?action=${encodeURIComponent(action)}` : "";
    return request(`/api/v1/admin/audit${query}`, { method: "GET" }, z.array(AuditEventSchema));
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
