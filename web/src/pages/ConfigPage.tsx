import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { ApiError, api, formatApiError, type ConfigRevision } from "../api/client";

type EditableConfig = {
  models: Record<string, unknown>;
  agents: unknown[];
};

const EMPTY_CONFIG: EditableConfig = { models: {}, agents: [] };

function formatDocument(document: EditableConfig) {
  return `${JSON.stringify(document, null, 2)}\n`;
}

function parseConfigJson(value: string): EditableConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    const message = error instanceof Error ? error.message : "格式错误";
    throw new Error(`JSON 解析失败：${message}`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("JSON 解析失败：配置根节点必须是对象。");
  }
  const document = parsed as Record<string, unknown>;
  if (typeof document.models !== "object" || document.models === null || Array.isArray(document.models)) {
    throw new Error("配置校验失败：models 必须是对象。");
  }
  if (!Array.isArray(document.agents)) {
    throw new Error("配置校验失败：agents 必须是数组。");
  }
  return {
    models: document.models as Record<string, unknown>,
    agents: document.agents,
  };
}

function currentOrEmpty(revision: ConfigRevision | undefined, error: unknown) {
  if (revision) return revision.document;
  if (error instanceof ApiError && error.status === 404) return EMPTY_CONFIG;
  return null;
}

export function ConfigPage() {
  const queryClient = useQueryClient();
  const current = useQuery({ queryKey: ["config-current"], queryFn: () => api.currentConfig() });
  const document = useMemo(
    () => currentOrEmpty(current.data, current.error),
    [current.data, current.error],
  );
  const [json, setJson] = useState(formatDocument(EMPTY_CONFIG));
  const [localError, setLocalError] = useState<string | null>(null);
  const [published, setPublished] = useState<string | null>(null);

  useEffect(() => {
    if (document) setJson(formatDocument(document));
  }, [document]);

  const publish = useMutation({
    mutationFn: async () => {
      setLocalError(null);
      setPublished(null);
      const parsed = parseConfigJson(json);
      const draft = await api.createConfigDraft(parsed);
      return api.publishConfigDraft(draft.id);
    },
    onSuccess: async (revision) => {
      setPublished(`已发布配置版本 ${revision.version}`);
      await queryClient.invalidateQueries({ queryKey: ["config-current"] });
      await queryClient.invalidateQueries({ queryKey: ["models"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (error) => {
      if (error instanceof Error && !(error instanceof ApiError)) {
        setLocalError(error.message);
      }
    },
  });

  if (current.isLoading) return <p>正在加载生产配置...</p>;
  if (current.isError && !(current.error instanceof ApiError && current.error.status === 404)) {
    return <p role="alert">{formatApiError(current.error, "生产配置加载失败")}</p>;
  }

  const modelCount = Object.keys(document?.models ?? {}).length;
  const agentCount = document?.agents.length ?? 0;

  return (
    <section>
      <p className="eyebrow">Production config</p>
      <h2>生产配置中心</h2>
      <p>
        这里编辑的是正式发布配置。模型建议优先在“模型”页面添加，系统会先真实请求模型 API；
        Agent 建议在“Agent”页面创建，避免手写字段出错。
      </p>

      <div className="status-grid" aria-label="配置状态">
        <article className="status-card">
          <span>当前发布版本</span>
          <p>{current.data ? `当前发布版本：${current.data.version}` : "当前没有已发布配置"}</p>
        </article>
        <article className="status-card">
          <span>模型数量</span>
          <p>{modelCount}</p>
        </article>
        <article className="status-card">
          <span>Agent 数量</span>
          <p>{agentCount}</p>
        </article>
      </div>

      <article>
        <h3>配置指引</h3>
        <ol>
          <li>先到“模型”页面选择服务商、模型、API Base 和 API Key，保存前会自动测试可用性。</li>
          <li>再到“Agent”页面选择角色模板，绑定一个已经通过测试的逻辑模型。</li>
          <li>只有需要批量调整或排错时，才直接编辑下方 JSON。</li>
          <li>发布失败时页面会显示后端返回的错误码、HTTP 状态和错误 ID，便于查日志。</li>
        </ol>
      </article>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          publish.mutate();
        }}
        aria-label="发布生产配置"
      >
        <label htmlFor="config-json">配置 JSON</label>
        <textarea
          id="config-json"
          value={json}
          onChange={(event) => {
            setJson(event.target.value);
            setLocalError(null);
            setPublished(null);
          }}
          spellCheck={false}
        />
        <button type="submit" disabled={publish.isPending}>
          {publish.isPending ? "正在创建草稿并发布..." : "创建草稿并发布"}
        </button>
        {published ? <p role="status">{published}</p> : null}
        {localError ? <p role="alert">{localError}</p> : null}
        {publish.isError && !localError ? (
          <p role="alert">{formatApiError(publish.error, "配置发布失败")}</p>
        ) : null}
      </form>
    </section>
  );
}
