import type { RunDetail } from "../api/client";

type Diagnostic = RunDetail["failure_diagnostics"][number];

const phases = {
  inspection: "检查依赖", resolution: "解析依赖", freeze: "冻结依赖", publication: "发布依赖",
};
const progress = {
  unobserved: "尚无已确认进度", resolution_completed: "依赖解析完成", freeze_pending: "等待冻结结果",
  freeze_completed: "依赖冻结完成", publication_pending: "等待发布结果", publication_completed: "依赖发布完成",
};

export function resolutionTimeoutMeta(diagnostic: Diagnostic): string[] {
  const phase = diagnostic.resolution_phase;
  const observed = diagnostic.resolution_progress;
  if (!phase || !observed || !diagnostic.resolution_progress_evidence) return [];
  return [
    `解析阶段：${phases[phase]}`,
    `解析进度：${progress[observed]}`,
    diagnostic.resolution_progress_evidence === "validated_root_status"
      ? "进度依据：已校验的执行状态" : "进度依据：无已确认状态",
    diagnostic.original_deadline_expired === true ? "原期限：已到期"
      : diagnostic.original_deadline_expired === false ? "原期限：观察时未到期" : "原期限：是否已到期未知",
  ];
}
