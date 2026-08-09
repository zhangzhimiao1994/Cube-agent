import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError } from "../api/client";

export function SkillsPage() {
  const [file, setFile] = useState<File | null>(null);
  const queryClient = useQueryClient();
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api.skills() });
  const upload = useMutation({
    mutationFn: () => {
      if (!file) throw new Error("请选择 Skill ZIP 文件");
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

  if (skills.isLoading) return <p>正在加载 Skill...</p>;
  if (skills.isError) {
    return <p role="alert">{formatApiError(skills.error, "Skill 加载失败")}</p>;
  }

  const items = skills.data ?? [];

  return (
    <section>
      <p className="eyebrow">Skill governance</p>
      <h2>技能管理</h2>
      <p>
        Skill 必须上传 ZIP 包并经过扫描，只有审批启用后才会进入可用列表。
        主 Agent 可以按任务分发给子 Agent，但不会绕过权限边界。
      </p>

      <div className="two-column">
        <article>
          <h3>上传并扫描 Skill</h3>
          <label>
            Skill ZIP
            <input
              aria-label="Skill ZIP"
              type="file"
              accept=".zip"
              onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
            />
          </label>
          <p className="field-help">
            只接受 `.zip`。后端会读取真实压缩包内容、解析清单、计算哈希并保存归档。
          </p>
          <button type="button" disabled={!file || upload.isPending} onClick={() => upload.mutate()}>
            {upload.isPending ? "正在扫描..." : "上传并扫描"}
          </button>
          {upload.isError ? <p role="alert">{formatApiError(upload.error, "Skill 上传失败")}</p> : null}
          {approve.isError ? <p role="alert">{formatApiError(approve.error, "Skill 审批失败")}</p> : null}
        </article>

        <article>
          <h3>配置指引</h3>
          <ol>
            <li>上传前确认 ZIP 中包含有效的 Skill 清单和入口文件。</li>
            <li>扫描结果会显示新增内容、入口和请求权限。</li>
            <li>审批前重点检查 requested permissions，危险权限不要直接启用。</li>
          </ol>
        </article>
      </div>

      <section aria-label="已上传 Skill">
        <h3>已上传 Skill</h3>
        {items.length === 0 ? (
          <article>
            <h4>还没有 Skill</h4>
            <p>从上方上传 ZIP 包，扫描成功后会显示在这里。</p>
          </article>
        ) : (
          <div className="card-grid">
            {items.map((skill) => (
              <article key={skill.id}>
                <span className="eyebrow">{skill.status}</span>
                <h3>{skill.name}</h3>
                <p>ID：{skill.id}</p>
                <p>扫描结果：{skill.scan_diff.join("; ") || "无"}</p>
                <p>请求权限：{skill.requested_permissions.join(", ") || "无"}</p>
                <button
                  type="button"
                  disabled={skill.status === "enabled" || approve.isPending}
                  onClick={() => approve.mutate(skill.id)}
                >
                  {skill.status === "enabled" ? "已启用" : "审批并启用"}
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
