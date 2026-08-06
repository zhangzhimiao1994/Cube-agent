import type { ConfigDiff as ConfigDiffType } from "../api/client";

export function ConfigDiff({ diff }: { diff: ConfigDiffType }) {
  return (
    <dl>
      <dt>新增</dt>
      <dd>{diff.added.join(", ") || "无"}</dd>
      <dt>移除</dt>
      <dd>{diff.removed.join(", ") || "无"}</dd>
      <dt>变更</dt>
      <dd>{diff.changed.join(", ") || "无"}</dd>
    </dl>
  );
}
