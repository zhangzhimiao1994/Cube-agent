import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError, type Skill } from "../api/client";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

type SkillSortKey = "name" | "status" | "scan" | "permissions";

type SkillColumnFilters = {
  name: string;
  permissions: string;
  scan: string;
  status: string;
};

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

export function SkillsPage() {
  const [file, setFile] = useState<File | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [searchTerm, setSearchTerm] = useState("");
  const [columnFilters, setColumnFilters] = useState<SkillColumnFilters>(EMPTY_SKILL_FILTERS);
  const [sort, setSort] = useState<SortState<SkillSortKey>>({ key: "name", direction: "asc" });
  const queryClient = useQueryClient();
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api.skills() });
  const upload = useMutation({
    mutationFn: () => {
      if (!file) throw new Error("请选择 Skill 压缩包");
      return api.uploadSkillArchive(file);
    },
    onSuccess: () => {
      setFile(null);
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
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
    mutationFn: async (ids: string[]) => {
      await Promise.all(ids.map((id) => api.deleteSkill(id)));
      return ids;
    },
    onSuccess: () => {
      setSelectedIds([]);
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
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
  const busy = approve.isPending || deleteSkill.isPending || bulkApprove.isPending || bulkDelete.isPending;
  const skippedUploadItems = upload.data?.skipped ?? [];

  return (
    <section>
      <p className="eyebrow">Skill governance</p>
      <h2>技能管理</h2>
      <p>
        Skill 必须上传压缩包并经过扫描，只有审批启用后才会进入可用列表。
        主 Agent 可以按任务分发给子 Agent，但不会绕过权限边界。
      </p>

      <div className="two-column">
        <article>
          <h3>上传并扫描 Skill</h3>
          <label>
            Skill 压缩包
            <input
              aria-label="Skill 压缩包"
              type="file"
              accept=".zip,.tar,.tar.gz,.tgz"
              onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
            />
          </label>
          <p className="field-help">
            接受 `.zip`、`.tar`、`.tar.gz`、`.tgz`。可以上传单个可执行 Skill，也可以上传包含多个 Skill 目录的归档；指令型 Skill 目录需包含 `SKILL.md`。
          </p>
          <button type="button" disabled={!file || upload.isPending} onClick={() => upload.mutate()}>
            {upload.isPending ? "正在扫描..." : "上传并扫描"}
          </button>
          {upload.isError ? <p role="alert">{formatApiError(upload.error, "Skill 上传失败")}</p> : null}
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
        </article>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>可执行 Skill 需包含 `skill.yaml`/`skill.json` 和入口文件；指令型 Skill 需包含 `SKILL.md`。</li>
            <li>多 Skill 归档请把每个 Skill 放在独立目录中，系统会逐个扫描并入库。</li>
            <li>扫描结果会显示包类型、入口或 `SKILL.md`、内容哈希和请求权限。</li>
            <li>审批前重点检查 requested permissions，危险权限不要直接启用。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已上传 Skill">
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
            <div className="bulk-action-bar">
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
              <label className="inline-check compact-check">
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