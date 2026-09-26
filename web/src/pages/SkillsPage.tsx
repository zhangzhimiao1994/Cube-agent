import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import {
  ApiError,
  api,
  formatApiError,
  type Skill,
  type SkillSource,
  type SkillSourceCreateInput,
  type SkillSourceRevision,
} from "../api/client";
import { useNavSection } from "../app/navSections";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

type SkillSortKey = "name" | "status" | "scan" | "permissions";

type SkillColumnFilters = {
  name: string;
  permissions: string;
  scan: string;
  status: string;
};

type UploadStrategy = "overwrite" | "new_version";

type SkillUploadConflict = {
  file: File;
  name: string;
};

const EMPTY_SOURCE_FORM: SkillSourceCreateInput = {
  name: "",
  repository_url: "",
  ref: "main",
  subdirectory: "",
  enabled: true,
  credential_ref: null,
  expected_commit_sha: null,
  expected_archive_sha256: null,
};

const TRUST_LABELS: Record<SkillSource["trust_state"], string> = {
  trusted: "已信任",
  untrusted: "未信任",
  revoked: "已撤销",
};

const SYNC_LABELS: Record<SkillSource["sync_state"], string> = {
  never: "尚未同步",
  syncing: "同步中",
  succeeded: "同步成功",
  failed: "同步失败",
};

function compactHash(value: string | null | undefined) {
  return value ? value.slice(0, 12) : "未生成";
}

function optionalValue(value: string | null | undefined) {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function revisionMemberApproved(skills: Skill[], skillName: string, versionId: string) {
  const skill = skills.find((item) => item.name === skillName || item.id === skillName);
  return skill?.versions.some((version) => version.id === versionId && version.status === "enabled") ?? false;
}

function revisionReady(revision: SkillSourceRevision, skills: Skill[]) {
  return revision.items.length > 0 && revision.items.every((item) => revisionMemberApproved(skills, item.skill_name, item.version_id));
}

type SkillSourceEntryProps = {
  source: SkillSource;
  skills: Skill[];
  busy: boolean;
  onEdit: (source: SkillSource) => void;
  onTrust: (source: SkillSource, action: "trust" | "revoke") => void;
  onSync: (sourceId: string) => void;
  onDelete: (source: SkillSource) => void;
};

function SkillSourceEntry({ source, skills, busy, onEdit, onTrust, onSync, onDelete }: SkillSourceEntryProps) {
  const queryClient = useQueryClient();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [snapshotFile, setSnapshotFile] = useState<File | null>(null);
  const revisionKey = ["skill-source-revisions", source.id];
  const revisions = useQuery({
    queryKey: revisionKey,
    queryFn: () => api.skillSourceRevisions(source.id),
    enabled: historyOpen,
  });
  const updateRevisionCache = (updated: SkillSourceRevision) => {
    queryClient.setQueryData<SkillSourceRevision[]>(revisionKey, (current = []) =>
      current.map((revision) => ({ ...revision, is_active: revision.id === updated.id ? updated.is_active : false })),
    );
    void queryClient.invalidateQueries({ queryKey: ["skill-sources"] });
  };
  const importSnapshot = useMutation({
    mutationFn: () => api.importSkillSourceSnapshot(source.id, source.expected_commit_sha ?? "", snapshotFile as File),
    onSuccess: () => {
      setSnapshotFile(null);
      setHistoryOpen(true);
      void queryClient.invalidateQueries({ queryKey: revisionKey });
      void queryClient.invalidateQueries({ queryKey: ["skill-sources"] });
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const activateRevision = useMutation({
    mutationFn: (revisionId: string) => api.activateSkillSourceRevision(source.id, revisionId),
    onSuccess: updateRevisionCache,
  });
  const rollbackRevision = useMutation({
    mutationFn: (revisionId: string) => api.rollbackSkillSourceRevision(source.id, revisionId),
    onSuccess: updateRevisionCache,
  });
  const revisionBusy = importSnapshot.isPending || activateRevision.isPending || rollbackRevision.isPending;
  const revisionError = importSnapshot.error ?? activateRevision.error ?? rollbackRevision.error;
  const snapshotReady = Boolean(
    snapshotFile && source.enabled && source.trust_state === "trusted" && source.expected_commit_sha && source.expected_archive_sha256,
  );

  return (
    <article className="skill-source-card">
      <div className="skill-source-card-heading">
        <div>
          <strong>{source.name}</strong>
          <small>{source.repository_url}</small>
        </div>
        <div className="skill-source-statuses">
          <span className={`status-pill ${source.trust_state === "trusted" ? "success" : "warning"}`}>{TRUST_LABELS[source.trust_state]}</span>
          <span className={`status-pill ${source.sync_state === "succeeded" ? "success" : source.sync_state === "failed" ? "danger" : ""}`}>{SYNC_LABELS[source.sync_state]}</span>
          {source.active_revision_id ? <span className="status-pill success">已有生效版本</span> : null}
          {!source.enabled ? <span className="status-pill">已停用</span> : null}
        </div>
      </div>
      <dl className="skill-source-summary">
        <div><dt>引用</dt><dd>{source.ref}{source.subdirectory ? ` / ${source.subdirectory}` : ""}</dd></div>
        <div><dt>固定 Commit</dt><dd className="mono-copy">{compactHash(source.expected_commit_sha)}</dd></div>
        <div><dt>最近 Commit</dt><dd className="mono-copy">{compactHash(source.resolved_commit_sha)}</dd></div>
        <div><dt>归档哈希</dt><dd className="mono-copy">{compactHash(source.archive_sha256)}</dd></div>
        <div><dt>关联 Skill</dt><dd>{source.linked_skill_ids.join("、") || "暂无"}</dd></div>
      </dl>
      {source.trust_reason ? <p className="field-help">信任说明：{source.trust_reason}</p> : null}
      {source.last_error ? <p className="danger-text">最近错误：{source.last_error}</p> : null}
      <div className="table-actions">
        <button type="button" className="secondary-action" disabled={busy} aria-label={`编辑${source.name}`} onClick={() => onEdit(source)}>编辑</button>
        <button type="button" disabled={busy} aria-label={`${source.trust_state === "trusted" ? "重新信任" : "信任"}${source.name}`} onClick={() => onTrust(source, "trust")}>{source.trust_state === "trusted" ? "重新信任" : "信任"}</button>
        <button type="button" className="secondary-action" disabled={busy || source.trust_state !== "trusted"} aria-label={`撤销信任${source.name}`} onClick={() => onTrust(source, "revoke")}>撤销信任</button>
        <button type="button" disabled={busy || !source.enabled || source.trust_state !== "trusted"} aria-label={`同步${source.name}`} onClick={() => onSync(source.id)}>同步</button>
        <button type="button" className="secondary-action" aria-expanded={historyOpen} aria-label={`${historyOpen ? "收起" : "查看"}${source.name}版本历史`} onClick={() => setHistoryOpen((current) => !current)}>{historyOpen ? "收起版本" : "版本历史"}</button>
        <button type="button" className="danger-action" disabled={busy} aria-label={`删除${source.name}`} onClick={() => onDelete(source)}>删除</button>
      </div>

      <form className="skill-source-snapshot" onSubmit={(event) => { event.preventDefault(); if (snapshotReady) importSnapshot.mutate(); }}>
        <label>
          可信快照 ZIP
          <input aria-label="可信快照 ZIP" type="file" accept=".zip,application/zip" onChange={(event) => setSnapshotFile(event.currentTarget.files?.[0] ?? null)} />
        </label>
        <button type="submit" disabled={!snapshotReady || revisionBusy} aria-label={`导入${source.name}可信快照`}>
          {importSnapshot.isPending ? "正在导入..." : "导入可信快照"}
        </button>
        {!source.expected_commit_sha || !source.expected_archive_sha256 ? <p className="field-help">先配置预期 Commit SHA 与归档 SHA-256，才能离线导入。</p> : null}
      </form>

      {revisionError ? <p role="alert">{formatApiError(revisionError, "版本操作失败")}</p> : null}
      {historyOpen ? (
        <section className="skill-source-revisions" aria-label={`${source.name}版本历史`}>
          <header className="skill-source-revision-header">
            <strong>版本历史</strong>
            <span>{revisions.data ? `共 ${revisions.data.length} 个版本` : "正在读取"}</span>
          </header>
          {revisions.isLoading ? <p>正在加载版本历史...</p> : null}
          {revisions.isError ? <p role="alert">{formatApiError(revisions.error, "版本历史加载失败")}</p> : null}
          {revisions.data?.length === 0 ? <p className="field-help">还没有可管理的来源版本。</p> : null}
          <div className="skill-source-revision-list">
            {revisions.data?.map((revision) => {
              const ready = revisionReady(revision, skills);
              return (
                <div className="skill-source-revision-row" key={revision.id}>
                  <div className="skill-source-revision-meta">
                    <strong>{compactHash(revision.commit_sha)}</strong>
                    <span>{new Date(revision.created_at).toLocaleString("zh-CN")}</span>
                    <span>{revision.items.length} 个成员</span>
                    <span className={ready ? "success-text" : "warning-text"}>{ready ? "已审批，可激活" : "待审批，暂不可激活"}</span>
                    {revision.is_active ? <span className="status-pill success">当前生效</span> : null}
                  </div>
                  <div className="skill-source-revision-members">
                    {revision.items.map((item) => {
                      const approved = revisionMemberApproved(skills, item.skill_name, item.version_id);
                      return (
                        <div key={`${revision.id}-${item.skill_name}-${item.version_id}`}>
                          <strong>{item.skill_name}</strong>
                          <span>{item.source_path || "来源根目录"}</span>
                          <span className={approved ? "success-text" : "warning-text"}>{approved ? "已审批" : "待审批"}</span>
                        </div>
                      );
                    })}
                  </div>
                  <div className="table-actions skill-source-revision-actions">
                    <button type="button" disabled={!ready || revision.is_active || revisionBusy} aria-label={`激活版本 ${revision.id}`} onClick={() => activateRevision.mutate(revision.id)}>激活</button>
                    <button type="button" className="secondary-action" disabled={!revision.is_active || !revision.previous_revision_id || revisionBusy} aria-label={`回滚版本 ${revision.id}`} onClick={() => rollbackRevision.mutate(revision.id)}>回滚</button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}
    </article>
  );
}

function SkillSourcesPanel() {
  const queryClient = useQueryClient();
  const sources = useQuery({ queryKey: ["skill-sources"], queryFn: () => api.skillSources() });
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api.skills() });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<SkillSourceCreateInput>(EMPTY_SOURCE_FORM);

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["skill-sources"] });
  const createSource = useMutation({ mutationFn: api.createSkillSource, onSuccess: () => { setForm(EMPTY_SOURCE_FORM); refresh(); } });
  const updateSource = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: SkillSourceCreateInput }) =>
      api.updateSkillSource(id, {
        name: payload.name,
        ref: payload.ref,
        subdirectory: payload.subdirectory,
        enabled: payload.enabled,
        credential_ref: payload.credential_ref,
        expected_commit_sha: payload.expected_commit_sha,
        expected_archive_sha256: payload.expected_archive_sha256,
      }),
    onSuccess: () => { setEditingId(null); setForm(EMPTY_SOURCE_FORM); refresh(); },
  });
  const trustSource = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => api.trustSkillSource(id, reason),
    onSuccess: refresh,
  });
  const revokeTrust = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => api.revokeSkillSourceTrust(id, reason),
    onSuccess: refresh,
  });
  const syncSource = useMutation({
    mutationFn: api.syncSkillSource,
    onSuccess: () => {
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const deleteSource = useMutation({ mutationFn: api.deleteSkillSource, onSuccess: refresh });
  const busy = createSource.isPending || updateSource.isPending || trustSource.isPending || revokeTrust.isPending || syncSource.isPending || deleteSource.isPending;
  const mutationError = createSource.error ?? updateSource.error ?? trustSource.error ?? revokeTrust.error ?? syncSource.error ?? deleteSource.error;

  function setField<K extends keyof SkillSourceCreateInput>(key: K, value: SkillSourceCreateInput[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function edit(source: SkillSource) {
    setEditingId(source.id);
    setForm({
      name: source.name,
      repository_url: source.repository_url,
      ref: source.ref,
      subdirectory: source.subdirectory,
      enabled: source.enabled,
      credential_ref: source.credential_ref ?? null,
      expected_commit_sha: source.expected_commit_sha ?? null,
      expected_archive_sha256: source.expected_archive_sha256 ?? null,
    });
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload: SkillSourceCreateInput = {
      ...form,
      name: form.name.trim(),
      repository_url: form.repository_url.trim(),
      ref: form.ref?.trim() || "main",
      subdirectory: form.subdirectory?.trim() || "",
      credential_ref: optionalValue(form.credential_ref),
      expected_commit_sha: optionalValue(form.expected_commit_sha),
      expected_archive_sha256: optionalValue(form.expected_archive_sha256),
    };
    if (editingId && !form.credential_ref?.trim()) delete payload.credential_ref;
    if (editingId) updateSource.mutate({ id: editingId, payload });
    else createSource.mutate(payload);
  }

  function requestReason(source: SkillSource, action: "trust" | "revoke") {
    const verb = action === "trust" ? "信任" : "撤销信任";
    const reason = window.prompt(`请输入${verb}「${source.name}」的理由`);
    if (!reason?.trim()) return;
    if (action === "trust") trustSource.mutate({ id: source.id, reason: reason.trim() });
    else revokeTrust.mutate({ id: source.id, reason: reason.trim() });
  }

  return (
    <section className="skill-source-panel" aria-label="团队 Skill 来源">
      <header className="skill-source-panel-header">
        <div>
          <span className="eyebrow">Team Skill Tap</span>
          <h3>团队 Skill 来源</h3>
        </div>
        <p className="field-help">同步只生成待审批候选，不会自动激活；审批后仍需在版本区手动选择生效版本。</p>
      </header>

      <form className="skill-source-form" onSubmit={submit}>
        <label>
          来源名称
          <input aria-label="来源名称" required value={form.name} onChange={(event) => setField("name", event.currentTarget.value)} />
        </label>
        <label className="skill-source-url-field">
          GitHub 仓库地址
          <input
            aria-label="GitHub 仓库地址"
            required
            readOnly={editingId !== null}
            value={form.repository_url}
            onChange={(event) => setField("repository_url", event.currentTarget.value)}
            placeholder="https://github.com/org/repo"
          />
        </label>
        <label>
          分支或标签
          <input value={form.ref} onChange={(event) => setField("ref", event.currentTarget.value)} />
        </label>
        <label>
          Skill 子目录
          <input value={form.subdirectory} onChange={(event) => setField("subdirectory", event.currentTarget.value)} placeholder="可留空" />
        </label>
        <label>
          凭据引用
          <input value={form.credential_ref ?? ""} onChange={(event) => setField("credential_ref", event.currentTarget.value)} placeholder="私有仓库可填" />
        </label>
        <label>
          预期 Commit SHA
          <input aria-label="预期 Commit SHA" maxLength={40} pattern="[0-9a-fA-F]{40}" value={form.expected_commit_sha ?? ""} onChange={(event) => setField("expected_commit_sha", event.currentTarget.value)} placeholder="40 位 commit SHA" />
        </label>
        <label>
          预期归档 SHA-256
          <input maxLength={64} pattern="[0-9a-fA-F]{64}" value={form.expected_archive_sha256 ?? ""} onChange={(event) => setField("expected_archive_sha256", event.currentTarget.value)} placeholder="64 位 SHA-256，可留空" />
        </label>
        <label className="inline-check compact-check skill-source-enabled">
          <input type="checkbox" checked={form.enabled} onChange={(event) => setField("enabled", event.currentTarget.checked)} />
          启用来源
        </label>
        <div className="channel-config-actions skill-source-form-actions">
          <button type="submit" disabled={busy || !form.name.trim() || !form.repository_url.trim()}>
            {editingId ? "保存来源" : "注册来源"}
          </button>
          {editingId ? (
            <button type="button" className="secondary-action" onClick={() => { setEditingId(null); setForm(EMPTY_SOURCE_FORM); }}>
              取消编辑
            </button>
          ) : null}
        </div>
      </form>

      {mutationError ? <p role="alert">{formatApiError(mutationError, "Skill 来源操作失败")}</p> : null}
      {sources.isLoading ? <p>正在加载团队 Skill 来源...</p> : null}
      {sources.isError ? <p role="alert">{formatApiError(sources.error, "Skill 来源加载失败")}</p> : null}
      {sources.data?.length === 0 ? <p className="field-help">还没有团队 Skill 来源。</p> : null}
      <div className="skill-source-list">
        {sources.data?.map((source) => (
          <SkillSourceEntry
            key={source.id}
            source={source}
            skills={skills.data ?? []}
            busy={busy}
            onEdit={edit}
            onTrust={requestReason}
            onSync={(sourceId) => syncSource.mutate(sourceId)}
            onDelete={(item) => { if (window.confirm(`确定删除来源「${item.name}」吗？已同步的 Skill 版本不会随之删除。`)) deleteSource.mutate(item.id); }}
          />
        ))}
      </div>
    </section>
  );
}

const EMPTY_SKILL_FILTERS: SkillColumnFilters = {
  name: "",
  permissions: "",
  scan: "",
  status: "all",
};

function toggle(values: string[], value: string) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function skillColumnValue(skill: Skill, key: SkillSortKey) {
  if (key === "name") return `${skill.name} ${skill.id}`;
  if (key === "status") return skill.status;
  if (key === "scan") return skill.scan_diff.join("; ");
  return skill.requested_permissions.join(", ");
}

function matchesSkillSearch(skill: Skill, query: string) {
  return textContains(
    [skill.id, skill.name, skill.status, ...skill.scan_diff, ...skill.requested_permissions].join(" "),
    query,
  );
}

function matchesSkillColumns(skill: Skill, filters: SkillColumnFilters) {
  return (
    textContains(`${skill.name} ${skill.id}`, filters.name) &&
    (filters.status === "all" || skill.status === filters.status) &&
    textContains(skill.scan_diff.join("; "), filters.scan) &&
    textContains(skill.requested_permissions.join(", "), filters.permissions)
  );
}

function sortedSkills(items: Skill[], sort: SortState<SkillSortKey>) {
  return [...items].sort((left, right) => compareText(skillColumnValue(left, sort.key), skillColumnValue(right, sort.key), sort.direction));
}

function skillCreatorObjective(goal: string, materials: string, checks: string) {
  return [
    `目标：${goal.trim()}`,
    `资料来源：${materials.trim()}`,
    `验收任务：${checks.trim()}`,
    "交付要求：生成可安装的 SKILL.md，并按需要沉淀 references、scripts、assets；所有外部资料必须保留来源，候选 Skill 必须经过真实任务验收后才能审批启用。",
  ].join("\n");
}

function versionCount(skill: Skill) {
  return skill.versions.length || (skill.current_version_id ? 1 : 0);
}

function currentVersionLabel(skill: Skill) {
  return skill.current_version_id ?? skill.package_version_id ?? skill.content_sha256?.slice(0, 12) ?? "旧数据未提供";
}

function conflictName(error: ApiError, file: File) {
  const detailSkillName = error.details?.skill_name;
  const detailName = error.details?.name;
  const detailSkillId = error.details?.skill_id;
  if (typeof detailSkillName === "string" && detailSkillName) return detailSkillName;
  if (typeof detailName === "string" && detailName) return detailName;
  if (typeof detailSkillId === "string" && detailSkillId) return detailSkillId;
  return file.name;
}

export function SkillsPage() {
  const { navTargetProps } = useNavSection(["view"]);
  const [file, setFile] = useState<File | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [searchTerm, setSearchTerm] = useState("");
  const [columnFilters, setColumnFilters] = useState<SkillColumnFilters>(EMPTY_SKILL_FILTERS);
  const [sort, setSort] = useState<SortState<SkillSortKey>>({ key: "name", direction: "asc" });
  const [creatorTopic, setCreatorTopic] = useState("AI 科研 Skill");
  const [creatorGoal, setCreatorGoal] = useState("围绕 AI 方向完成资料检索、研究问题拆解、创新点发现和论文计划输出。");
  const [creatorMaterials, setCreatorMaterials] = useState("由主 Agent 先制定检索计划，再拉取真实论文、项目和基准资料；用户可补充本地文献或链接。");
  const [creatorChecks, setCreatorChecks] = useState("用 3 个真实研究任务验收：资料综述、创新点候选、论文实验计划。未通过则继续迭代。");
  const [uploadConflict, setUploadConflict] = useState<SkillUploadConflict | null>(null);
  const queryClient = useQueryClient();
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api.skills() });
  const upload = useMutation({
    mutationFn: (strategy?: UploadStrategy) => {
      const uploadFile = strategy ? uploadConflict?.file : file;
      if (!uploadFile) throw new Error("请选择 Skill 压缩包");
      return api.uploadSkillArchive(uploadFile, strategy);
    },
    onSuccess: () => {
      setFile(null);
      setUploadConflict(null);
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (error) => {
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === "skill_version_choice_required" &&
        file
      ) {
        setUploadConflict({ file, name: conflictName(error, file) });
      }
    },
  });
  const activateVersion = useMutation({
    mutationFn: ({ skillId, versionId }: { skillId: string; versionId: string }) =>
      api.activateSkillVersion(skillId, versionId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["skills"] }),
  });
  const approve = useMutation({
    mutationFn: (id: string) => api.approveSkill(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["skills"] }),
  });
  const deleteSkill = useMutation({
    mutationFn: (id: string) => api.deleteSkill(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["skills"] }),
  });
  const bulkApprove = useMutation({
    mutationFn: async (ids: string[]) => {
      await Promise.all(ids.map((id) => api.approveSkill(id)));
      return ids;
    },
    onSuccess: () => {
      setSelectedIds([]);
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const bulkDelete = useMutation({
    mutationFn: (ids: string[]) => api.bulkDeleteSkills(ids),
    onSuccess: (result) => {
      const failedIds = new Set(result.failed.map((item) => item.id));
      setSelectedIds((current) => current.filter((id) => failedIds.has(id)));
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const createSkillTask = useMutation({
    mutationFn: () =>
      api.createEvolutionRun({
        kind: "skill_distillation",
        title: `创建 ${creatorTopic.trim()}`,
        objective: skillCreatorObjective(creatorGoal, creatorMaterials, creatorChecks),
        mode: "hybrid",
        source_skill_ids: [],
        target_artifact_type: "skill",
        baseline_agent_id: "main-agent",
        candidate_agent_ids: ["agent-researcher", "agent-skill-builder", "agent-evaluator"],
        evaluator_agent_id: "evaluator-agent",
        approval_policy: "ask",
        iteration_policy: "score_gated",
        memory_policy: "summarize_between_rounds",
        max_rounds: 5,
        min_delta: 2,
        budget_tokens: 200000,
        budget_minutes: 120,
        rubric: ["资料真实性", "可执行 Skill 结构", "真实任务验收", "权限边界"],
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["evolution-runs"] });
    },
  });

  function updateColumnFilter(key: keyof SkillColumnFilters, value: string) {
    setColumnFilters((current) => ({ ...current, [key]: value }));
  }

  function confirmDeleteSkill(id: string, name: string) {
    if (!window.confirm(`确定删除 Skill「${name}」吗？删除后不会再分发给主 Agent 或子 Agent。`)) return;
    deleteSkill.mutate(id);
  }

  function toggleAll(ids: string[]) {
    setSelectedIds((current) => {
      const allSelected = ids.length > 0 && ids.every((id) => current.includes(id));
      if (allSelected) return current.filter((id) => !ids.includes(id));
      return Array.from(new Set([...current, ...ids]));
    });
  }

  if (skills.isLoading) return <p>正在加载 Skill...</p>;
  if (skills.isError) {
    return <p role="alert">{formatApiError(skills.error, "Skill 加载失败")}</p>;
  }

  const items = skills.data ?? [];
  const filteredItems = items.filter((skill) => matchesSkillSearch(skill, searchTerm) && matchesSkillColumns(skill, columnFilters));
  const visibleItems = sortedSkills(filteredItems, sort);
  const visibleIds = visibleItems.map((skill) => skill.id);
  const visibleApprovalIds = visibleItems.filter((skill) => skill.status !== "enabled").map((skill) => skill.id);
  const selectedVisibleIds = selectedIds.filter((id) => visibleIds.includes(id));
  const selectedVisibleApprovalIds = selectedIds.filter((id) => visibleApprovalIds.includes(id));
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.includes(id));
  const allVisibleApprovalSelected =
    visibleApprovalIds.length > 0 && visibleApprovalIds.every((id) => selectedIds.includes(id));
  const busy =
    approve.isPending ||
    deleteSkill.isPending ||
    bulkApprove.isPending ||
    bulkDelete.isPending ||
    createSkillTask.isPending ||
    activateVersion.isPending;
  const skippedUploadItems = upload.data?.skipped ?? [];
  const showUploadError = upload.isError && !uploadConflict;

  return (
    <section>
      <p className="eyebrow">Skill governance</p>
      <h2>技能管理</h2>
      <p>
        Skill 必须上传压缩包并经过扫描，只有审批启用后才会进入可用列表。
        主 Agent 可以按任务分发给子 Agent，但不会绕过权限边界。
      </p>

      <section className="resource-card skill-creator-panel" aria-label="创建 Skill 任务">
        <div>
          <span className="eyebrow">Skill Creator</span>
          <h3>创建 Skill 任务</h3>
          <p className="field-help">把科研、自媒体、人物/书籍蒸馏等需求先落成可验收的进化任务。主 Agent 会先规划资料、候选结构和评测口径，再进入多轮迭代。</p>
        </div>
        <div className="form-grid">
          <label>
            Skill 方向
            <input value={creatorTopic} onChange={(event) => setCreatorTopic(event.currentTarget.value)} />
          </label>
          <label>
            目标
            <textarea value={creatorGoal} onChange={(event) => setCreatorGoal(event.currentTarget.value)} />
          </label>
          <label>
            资料来源
            <textarea value={creatorMaterials} onChange={(event) => setCreatorMaterials(event.currentTarget.value)} />
          </label>
          <label>
            验收任务
            <textarea value={creatorChecks} onChange={(event) => setCreatorChecks(event.currentTarget.value)} />
          </label>
        </div>
        <div className="channel-config-actions" role="group" aria-label="Skill 创建操作">
          <button
            type="button"
            disabled={createSkillTask.isPending || !creatorTopic.trim() || !creatorGoal.trim() || !creatorChecks.trim()}
            onClick={() => createSkillTask.mutate()}
          >
            {createSkillTask.isPending ? "创建中..." : "创建并进入进化"}
          </button>
          {createSkillTask.isSuccess ? <span role="status">已创建进化任务：{createSkillTask.data.title}。到“Skill 进化”继续审批和执行。</span> : null}
        </div>
        {createSkillTask.isError ? <p role="alert">{formatApiError(createSkillTask.error, "Skill 创建任务创建失败")}</p> : null}
      </section>

      <SkillSourcesPanel />

      <div className="two-column">
        <article {...navTargetProps("upload")}>
          <h3>上传并扫描 Skill</h3>
          <label>
            Skill 压缩包
            <input
              aria-label="Skill 压缩包"
              type="file"
              accept=".zip,.tar,.tar.gz,.tgz"
              onChange={(event) => {
                setFile(event.currentTarget.files?.[0] ?? null);
                setUploadConflict(null);
              }}
            />
          </label>
          <p className="field-help">
            支持 `.zip`、`.tar`、`.tar.gz`、`.tgz`。可以上传单个 Skill，也可以上传包含多个 Skill 目录的归档；多层外层文件夹会自动识别，每个指令型 Skill 目录需包含 `SKILL.md`。
          </p>
          <button type="button" disabled={!file || upload.isPending} onClick={() => upload.mutate(undefined)}>
            {upload.isPending ? "正在扫描..." : "上传并扫描"}
          </button>
          {uploadConflict ? (
            <div role="alert">
              <p>{uploadConflict.name} 已存在且内容不同。请选择覆盖当前版本，或保存为新版本。</p>
              <div className="channel-config-actions" role="group" aria-label="同名 Skill 上传策略">
                <button type="button" disabled={upload.isPending} onClick={() => upload.mutate("overwrite")}>
                  覆盖当前版本
                </button>
                <button type="button" disabled={upload.isPending} onClick={() => upload.mutate("new_version")}>
                  保存为新版本
                </button>
              </div>
            </div>
          ) : null}
          {showUploadError ? <p role="alert">{formatApiError(upload.error, "Skill 上传失败")}</p> : null}
          {upload.isSuccess ? (
            <p role="status">
              已扫描 {upload.data.items.length} 个 Skill
              {skippedUploadItems.length > 0 ? `，跳过 ${skippedUploadItems.length} 项：${skippedUploadItems.map((item) => `${item.path}（${item.reason}）`).join("；")}` : ""}
            </p>
          ) : null}
          {approve.isError ? <p role="alert">{formatApiError(approve.error, "Skill 审批失败")}</p> : null}
          {deleteSkill.isError ? <p role="alert">{formatApiError(deleteSkill.error, "Skill 删除失败")}</p> : null}
          {bulkApprove.isError ? <p role="alert">{formatApiError(bulkApprove.error, "Skill 批量审批失败")}</p> : null}
          {bulkDelete.isError ? <p role="alert">{formatApiError(bulkDelete.error, "Skill 批量删除失败")}</p> : null}
          {activateVersion.isError ? <p role="alert">{formatApiError(activateVersion.error, "Skill 版本激活失败")}</p> : null}
          {bulkDelete.isSuccess && bulkDelete.data.failed.length > 0 ? (
            <p role="status">已删除 {bulkDelete.data.deleted.length} 个 Skill，{bulkDelete.data.failed.length} 个未删除。</p>
          ) : null}
        </article>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>可执行 Skill 需包含 `skill.yaml`/`skill.json` 和入口文件；指令型 Skill 需包含 `SKILL.md`。</li>
            <li>多 Skill 归档请把每个 Skill 放在独立目录中；可以带 references、assets 或嵌套示例文件，系统会识别父 Skill 并逐个扫描。</li>
            <li>扫描结果会显示包类型、入口或 `SKILL.md`、内容哈希和请求权限。</li>
            <li>审批前重点检查 requested permissions，危险权限不要直接启用。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已上传 Skill" {...navTargetProps("installed")}>
        <h3>已上传 Skill</h3>
        {items.length === 0 ? (
          <article>
            <h4>还没有 Skill</h4>
            <p>从上方上传 Skill 压缩包，扫描成功后会显示在这里。</p>
          </article>
        ) : (
          <>
            <div className="list-toolbar">
              <label>
                快速搜索 Skill
                <input
                  type="search"
                  aria-label="快速搜索 Skill"
                  value={searchTerm}
                  onChange={(event) => setSearchTerm(event.currentTarget.value)}
                  placeholder="跨名称、ID、状态、权限和扫描结果搜索"
                />
              </label>
              <button type="button" className="secondary-action" onClick={() => { setSearchTerm(""); setColumnFilters(EMPTY_SKILL_FILTERS); }}>
                清空筛选
              </button>
              <small>
                显示 {visibleItems.length} / {items.length}
              </small>
            </div>
            <div {...navTargetProps("bulk", "bulk-action-bar")}>
              <label className="inline-check compact-check">
                <input
                  type="checkbox"
                  aria-label="全选当前结果 Skill"
                  checked={allVisibleSelected}
                  disabled={visibleIds.length === 0 || busy}
                  onChange={() => toggleAll(visibleIds)}
                />
                全选当前结果
              </label>
              <label {...navTargetProps("permissions", "inline-check compact-check")}>
                <input
                  type="checkbox"
                  aria-label="全选当前待审批 Skill"
                  checked={allVisibleApprovalSelected}
                  disabled={visibleApprovalIds.length === 0 || busy}
                  onChange={() => toggleAll(visibleApprovalIds)}
                />
                全选待审批
              </label>
              <button
                type="button"
                className="secondary-action"
                disabled={selectedVisibleApprovalIds.length === 0 || busy}
                onClick={() => bulkApprove.mutate(selectedVisibleApprovalIds)}
              >
                {bulkApprove.isPending ? "审批中..." : `批量审批待审批 Skill（${selectedVisibleApprovalIds.length}）`}
              </button>
              <button
                type="button"
                className="danger-button"
                disabled={selectedVisibleIds.length === 0 || busy}
                onClick={() => {
                  if (!window.confirm(`确认删除当前结果中已选的 ${selectedVisibleIds.length} 个 Skill？删除后不会再分发给主 Agent 或子 Agent。`)) {
                    return;
                  }
                  bulkDelete.mutate(selectedVisibleIds);
                }}
              >
                {bulkDelete.isPending ? "删除中..." : `批量删除已选 Skill（${selectedVisibleIds.length}）`}
              </button>
              <small>当前结果已选 {selectedVisibleIds.length}</small>
            </div>
            {visibleItems.length === 0 ? (
              <article>
                <h4>没有匹配的 Skill</h4>
                <p>调整列筛选或清空筛选查看全部 Skill。</p>
              </article>
            ) : (
              <div className="table-shell">
                <table aria-label="已上传 Skill">
                  <thead>
                    <tr>
                      <th>选择</th>
                      <th><SortHeader column="name" label="Skill" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>Skill</SortHeader></th>
                      <th><SortHeader column="status" label="状态" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>状态</SortHeader></th>
                      <th><SortHeader column="scan" label="扫描结果" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>扫描结果</SortHeader></th>
                      <th><SortHeader column="permissions" label="请求权限" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>请求权限</SortHeader></th>
                      <th>操作</th>
                    </tr>
                    <tr className="table-filter-row">
                      <th></th>
                      <th>
                        <input aria-label="按 Skill 筛选" value={columnFilters.name} onChange={(event) => updateColumnFilter("name", event.currentTarget.value)} placeholder="名称或 ID" />
                      </th>
                      <th>
                        <select aria-label="按 Skill 状态筛选" value={columnFilters.status} onChange={(event) => updateColumnFilter("status", event.currentTarget.value)}>
                          <option value="all">全部</option>
                          <option value="quarantined">quarantined</option>
                          <option value="scanned">scanned</option>
                          <option value="approved">approved</option>
                          <option value="enabled">enabled</option>
                          <option value="disabled">disabled</option>
                        </select>
                      </th>
                      <th>
                        <input aria-label="按 Skill 扫描结果筛选" value={columnFilters.scan} onChange={(event) => updateColumnFilter("scan", event.currentTarget.value)} placeholder="扫描关键词" />
                      </th>
                      <th>
                        <input aria-label="按 Skill 请求权限筛选" value={columnFilters.permissions} onChange={(event) => updateColumnFilter("permissions", event.currentTarget.value)} placeholder="权限关键词" />
                      </th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleItems.map((skill) => (
                      <tr key={skill.id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`选择 Skill ${skill.id}`}
                            checked={selectedIds.includes(skill.id)}
                            disabled={busy}
                            onChange={() => setSelectedIds((current) => toggle(current, skill.id))}
                          />
                        </td>
                        <td>
                          <strong>{skill.name}</strong>
                          <p className="field-help">ID：{skill.id}</p>
                          <p className="field-help">当前版本：{currentVersionLabel(skill)}；版本数：{versionCount(skill)}</p>
                          {skill.source_filename ? <p className="field-help">来源：{skill.source_filename}</p> : null}
                          {skill.content_sha256 ? <p className="field-help">SHA256：{skill.content_sha256.slice(0, 12)}</p> : null}
                          {skill.source ? (
                            <p className="field-help skill-source-provenance">
                              来源提交：{compactHash(skill.source.commit_sha)} · {skill.source.repository_url}
                            </p>
                          ) : null}
                          {skill.versions.map((version) =>
                            version.source ? (
                              <p className="field-help skill-source-provenance" key={`${version.id}-source`}>
                                {version.id} 来源提交：{compactHash(version.source.commit_sha)}
                                {version.source.source_path ? ` · ${version.source.source_path}` : ""}
                              </p>
                            ) : null,
                          )}
                        </td>
                        <td>{skill.status}</td>
                        <td>{skill.scan_diff.join("; ") || "无"}</td>
                        <td>{skill.requested_permissions.join(", ") || "无"}</td>
                        <td className="table-actions">
                          <button
                            type="button"
                            disabled={skill.status === "enabled" || busy}
                            onClick={() => approve.mutate(skill.id)}
                          >
                            {skill.status === "enabled" ? "已启用" : "审批启用"}
                          </button>
                          <button
                            type="button"
                            className="danger-action"
                            disabled={busy}
                            onClick={() => confirmDeleteSkill(skill.id, skill.name)}
                          >
                            删除
                          </button>
                          {skill.versions.length > 1
                            ? skill.versions
                                .filter((version) => !version.is_current)
                                .map((version) => (
                                  <button
                                    key={version.id}
                                    type="button"
                                    className="secondary-action"
                                    disabled={busy}
                                    onClick={() => {
                                      if (version.status === "enabled") {
                                        activateVersion.mutate({ skillId: skill.id, versionId: version.id });
                                      } else {
                                        approve.mutate(version.id);
                                      }
                                    }}
                                  >
                                    {version.status === "enabled" ? `激活 ${version.id}` : `审批版本 ${version.id}`}
                                  </button>
                                ))
                            : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </section>
  );
}
