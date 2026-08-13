import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError, type Skill } from "../api/client";

function toggle(values: string[], value: string) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

export function SkillsPage() {
  const [file, setFile] = useState<File | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
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

  function selectedSkills(items: Skill[]) {
    return items.filter((item) => selectedIds.includes(item.id));
  }

  if (skills.isLoading) return <p>正在加载 Skill...</p>;
  if (skills.isError) {
    return <p role="alert">{formatApiError(skills.error, "Skill 加载失败")}</p>;
  }

  const items = skills.data ?? [];
  const quarantinedIds = items.filter((skill) => skill.status !== "enabled").map((skill) => skill.id);
  const selectedItems = selectedSkills(items);
  const selectedQuarantinedIds = selectedItems
    .filter((skill) => skill.status !== "enabled")
    .map((skill) => skill.id);
  const allQuarantinedSelected =
    quarantinedIds.length > 0 && quarantinedIds.every((id) => selectedIds.includes(id));
  const busy = approve.isPending || deleteSkill.isPending || bulkApprove.isPending || bulkDelete.isPending;

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
            <div className="bulk-action-bar">
              <label className="inline-check compact-check">
                <input
                  type="checkbox"
                  aria-label="全选待审批 Skill"
                  checked={allQuarantinedSelected}
                  disabled={quarantinedIds.length === 0 || busy}
                  onChange={() => toggleAll(quarantinedIds)}
                />
                全选待审批
              </label>
              <button
                type="button"
                className="secondary-action"
                disabled={selectedQuarantinedIds.length === 0 || busy}
                onClick={() => bulkApprove.mutate(selectedQuarantinedIds)}
              >
                {bulkApprove.isPending ? "审批中..." : "批量审批启用已选 Skill"}
              </button>
              <button
                type="button"
                className="danger-button"
                disabled={selectedIds.length === 0 || busy}
                onClick={() => {
                  if (!window.confirm(`确认删除 ${selectedIds.length} 个已选 Skill？删除后不会再分发给主 Agent 或子 Agent。`)) {
                    return;
                  }
                  bulkDelete.mutate(selectedIds);
                }}
              >
                {bulkDelete.isPending ? "删除中..." : "批量删除已选 Skill"}
              </button>
              <small>已选 {selectedIds.length}</small>
            </div>
            <div className="table-shell">
              <table aria-label="已上传 Skill">
                <thead>
                  <tr>
                    <th>选择</th>
                    <th>Skill</th>
                    <th>状态</th>
                    <th>扫描结果</th>
                    <th>请求权限</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((skill) => (
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
          </>
        )}
      </section>
    </section>
  );
}
