import { useMutation, useQuery } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, formatApiError, type ModelDeployment } from "../api/client";

type GenerationKind = "image" | "video";

const KIND_OPTIONS: Array<{ value: GenerationKind; label: string; capability: string }> = [
  { value: "image", label: "图片", capability: "image_generation" },
  { value: "video", label: "视频", capability: "video_generation" },
];

export function MultimediaPage() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.settings() });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const [kind, setKind] = useState<GenerationKind>("image");
  const [logicalModel, setLogicalModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const generate = useMutation({
    mutationFn: () =>
      api.generateMultimedia({
        kind,
        logical_model: logicalModel,
        prompt: prompt.trim(),
      }),
  });

  const capableModels = useMemo(
    () => uniqueLogicalModels(models.data ?? [], capabilityFor(kind)),
    [kind, models.data],
  );

  useEffect(() => {
    if (capableModels.length === 0) {
      setLogicalModel("");
      return;
    }
    if (!capableModels.some((model) => model.logical_model === logicalModel)) {
      setLogicalModel(capableModels[0].logical_model);
    }
  }, [capableModels, logicalModel]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings.data?.multimedia_generation_enabled) return;
    if (!logicalModel || !prompt.trim()) return;
    generate.mutate();
  }

  const disabled = !settings.data?.multimedia_generation_enabled;
  const blocked = disabled || capableModels.length === 0 || !logicalModel || !prompt.trim();

  return (
    <section>
      <p className="eyebrow">Multimedia executor</p>
      <h2>多媒体生成</h2>
      <p>图片和视频生成会先按模型能力过滤；视频请求只会提交给声明或识别为 video_generation 的模型。</p>

      {settings.isLoading || models.isLoading ? <p>正在加载配置...</p> : null}
      {settings.isError ? <p role="alert">{formatApiError(settings.error, "系统设置加载失败")}</p> : null}
      {models.isError ? <p role="alert">{formatApiError(models.error, "模型列表加载失败")}</p> : null}

      <form className="form-grid" onSubmit={submit} aria-label="多媒体生成表单">
        <fieldset>
          <legend>类型</legend>
          {KIND_OPTIONS.map((option) => (
            <label key={option.value} className="inline-check">
              <input
                type="radio"
                name="multimedia-kind"
                value={option.value}
                checked={kind === option.value}
                onChange={() => setKind(option.value)}
              />
              {option.label}
            </label>
          ))}
        </fieldset>
        <label htmlFor="multimedia-model">
          模型
          <select
            id="multimedia-model"
            value={logicalModel}
            onChange={(event) => setLogicalModel(event.target.value)}
            disabled={disabled || capableModels.length === 0}
          >
            {capableModels.map((model) => (
              <option key={model.logical_model} value={model.logical_model}>
                {model.logical_model} ({model.provider} / {model.upstream_model})
              </option>
            ))}
          </select>
        </label>
        <label htmlFor="multimedia-prompt">
          提示词
          <textarea
            id="multimedia-prompt"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            disabled={disabled}
            rows={6}
          />
        </label>
        <div className="toolbar">
          <button type="submit" disabled={generate.isPending || blocked}>
            {generate.isPending ? "生成中..." : "生成"}
          </button>
          <Link to="/config" className="secondary-action">
            系统设置
          </Link>
        </div>
      </form>

      {disabled ? <p role="status">多媒体生成开关已关闭。</p> : null}
      {!disabled && capableModels.length === 0 ? (
        <p role="status">当前没有具备 {capabilityFor(kind)} 能力的模型。</p>
      ) : null}
      {generate.isError ? <p role="alert">{formatApiError(generate.error, "多媒体生成失败")}</p> : null}
      {generate.data ? (
        <article>
          <p className="eyebrow">
            {generate.data.kind} / {generate.data.deployment_id}
          </p>
          <h3>{generate.data.logical_model}</h3>
          <p>{generate.data.text ?? "生成请求已提交。"}</p>
        </article>
      ) : null}
    </section>
  );
}

function capabilityFor(kind: GenerationKind) {
  return kind === "image" ? "image_generation" : "video_generation";
}

function uniqueLogicalModels(models: ModelDeployment[], capability: string) {
  const seen = new Set<string>();
  return models.filter((model) => {
    if (!model.capabilities.includes(capability) || seen.has(model.logical_model)) {
      return false;
    }
    seen.add(model.logical_model);
    return true;
  });
}
