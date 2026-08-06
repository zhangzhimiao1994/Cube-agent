import { useState } from "react";

import { api, type ConfigDiff as ConfigDiffType } from "../api/client";
import { ConfigDiff } from "../components/ConfigDiff";

export function ConfigPage() {
  const [yaml, setYaml] = useState("models: []\n");
  const [diff, setDiff] = useState<ConfigDiffType | null>(null);
  const [published, setPublished] = useState<string | null>(null);

  async function preview() {
    setDiff(await api.diffConfig(yaml));
  }

  async function publish() {
    await api.publishConfig(0);
    setPublished("已发布");
  }

  return (
    <section>
      <h2>配置</h2>
      <label>
        YAML 草稿
        <textarea value={yaml} onChange={(event) => setYaml(event.target.value)} />
      </label>
      <button type="button" onClick={() => void preview()}>
        查看 Diff
      </button>
      {diff && <ConfigDiff diff={diff} />}
      <button type="button" onClick={() => void publish()}>
        确认发布
      </button>
      {published && <p>{published}</p>}
    </section>
  );
}
