import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

export function ModelsPage() {
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  if (models.isLoading) return <p>加载模型...</p>;
  if (models.isError) return <p role="alert">模型加载失败</p>;
  return (
    <section>
      <h2>模型与并发</h2>
      <p>同一供应商账号的多个 Key 可能共享配额，不能重复计算容量。</p>
      {models.data?.map((model) => (
        <article key={model.id}>
          <h3>{model.logical_model}</h3>
          <p>最大并发：{model.effective_slots}</p>
          <p>满载策略：先排队，超时后降级</p>
          <p>Quota Scope：{model.quota_scope}</p>
        </article>
      ))}
    </section>
  );
}
