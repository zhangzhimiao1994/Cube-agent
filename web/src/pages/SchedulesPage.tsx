import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, formatApiError, type ScheduleCreatePayload } from "../api/client";

function localDateTimeWithShanghaiOffset(value: string): string {
  return `${value.length === 16 ? `${value}:00` : value}+08:00`;
}

export function SchedulesPage() {
  const queryClient = useQueryClient();
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: () => api.schedules() });
  const [name, setName] = useState("daily-report");
  const [message, setMessage] = useState("Open the report system and fill today's report");
  const [runAt, setRunAt] = useState("2026-08-13T09:00");
  const [tickStatus, setTickStatus] = useState("");
  const createSchedule = useMutation({
    mutationFn: (payload: ScheduleCreatePayload) => api.createSchedule(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });
  const tickSchedules = useMutation({
    mutationFn: () => api.tickSchedules(localDateTimeWithShanghaiOffset(runAt)),
    onSuccess: (result) => {
      setTickStatus(`已触发 ${result.fired.length} 个计划任务`);
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });

  const deleteSchedule = useMutation({
    mutationFn: (id: string) => api.deleteSchedule(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    const trimmedMessage = message.trim();
    if (!trimmedName || !trimmedMessage || !runAt) return;
    createSchedule.mutate({
      name: trimmedName,
      message: trimmedMessage,
      mode: "dispatch",
      workflow_id: "scheduled_task",
      kind: "one_time",
      run_at: localDateTimeWithShanghaiOffset(runAt),
      timezone: "Asia/Shanghai",
      misfire_policy: "fire_once",
      budget: 16384,
      metadata: { openclaw: "windows_desktop" },
    });
  }

  return (
    <section>
      <p className="eyebrow">Scheduled operations</p>
      <h2>计划任务</h2>
      <p>定时任务会按计划提交普通任务，不绕过模型能力、运行队列、OpenClaw 审批和审计边界。</p>

      <form className="form-grid" aria-label="计划任务表单" onSubmit={submit}>
        <label htmlFor="schedule-name">
          名称
          <input id="schedule-name" value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label htmlFor="schedule-message">
          执行指令
          <textarea
            id="schedule-message"
            rows={4}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
          />
        </label>
        <label htmlFor="schedule-run-at">
          执行时间
          <input
            id="schedule-run-at"
            type="datetime-local"
            value={runAt}
            onChange={(event) => setRunAt(event.target.value)}
          />
        </label>
        <div className="toolbar">
          <button type="submit" disabled={createSchedule.isPending}>
            {createSchedule.isPending ? "保存中..." : "保存计划任务"}
          </button>
          <button type="button" className="secondary-action" disabled={tickSchedules.isPending} onClick={() => tickSchedules.mutate()}>
            {tickSchedules.isPending ? "检查中..." : "立即检查到期任务"}
          </button>
        </div>
      </form>

      {createSchedule.isError ? <p role="alert">{formatApiError(createSchedule.error, "计划任务保存失败")}</p> : null}
      {tickSchedules.isError ? <p role="alert">{formatApiError(tickSchedules.error, "计划任务检查失败")}</p> : null}
      {tickStatus ? <p role="status">{tickStatus}</p> : null}

      {schedules.isLoading ? <p>正在加载计划任务...</p> : null}
      {schedules.isError ? <p role="alert">{formatApiError(schedules.error, "计划任务加载失败")}</p> : null}
      <div className="resource-list">
        {(schedules.data ?? []).map((schedule) => (
          <article key={schedule.id}>
            <p className="eyebrow">
              {schedule.status} / {schedule.kind} / {schedule.mode}
            </p>
            <h3>{schedule.name}</h3>
            <p>{schedule.message}</p>
            <dl>
              <dt>下次执行</dt>
              <dd>{schedule.next_fire_at ?? "无"}</dd>
              <dt>工作流</dt>
              <dd>{schedule.workflow_id}</dd>
              <dt>时区</dt>
              <dd>{schedule.timezone}</dd>
            </dl>
            <button
              type="button"
              className="danger-action"
              disabled={deleteSchedule.isPending}
              onClick={() => {
                if (window.confirm(`Delete scheduled task ${schedule.name}?`)) {
                  deleteSchedule.mutate(schedule.id);
                }
              }}
            >
              Delete scheduled task
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}
