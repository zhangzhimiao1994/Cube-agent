import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

import { api, formatApiError, type Schedule, type ScheduleCreatePayload } from "../api/client";

type Recurrence = "one_time" | "daily" | "weekly";

const WEEKDAYS = [
  { value: "1", label: "周一" },
  { value: "2", label: "周二" },
  { value: "3", label: "周三" },
  { value: "4", label: "周四" },
  { value: "5", label: "周五" },
  { value: "6", label: "周六" },
  { value: "0", label: "周日" },
] as const;

function localDateTimeWithShanghaiOffset(date: string, time: string): string {
  return `${date}T${time}:00+08:00`;
}

function cronFromTime(time: string, weekday?: string): string {
  const [hour = "9", minute = "0"] = time.split(":");
  return `${Number(minute)} ${Number(hour)} * * ${weekday ?? "*"}`;
}

function formatTime(hour: string, minute: string): string {
  return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
}

function describeSchedule(schedule: Schedule): string {
  if (schedule.kind === "one_time" && schedule.run_at) {
    return `一次性 ${schedule.run_at}`;
  }
  const match = schedule.cron?.match(/^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$/);
  if (!match) {
    return schedule.cron ?? "未设置";
  }
  const [, minute, hour, weekday] = match;
  const time = formatTime(hour, minute);
  if (weekday === "*") {
    return `每天 ${time}`;
  }
  const weekdayLabel = WEEKDAYS.find((item) => item.value === weekday)?.label ?? `周${weekday}`;
  return `每周${weekdayLabel.replace("周", "")} ${time}`;
}

export function SchedulesPage() {
  const queryClient = useQueryClient();
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: () => api.schedules() });
  const [name, setName] = useState("daily-report");
  const [message, setMessage] = useState("Open the report system and fill today's report");
  const [recurrence, setRecurrence] = useState<Recurrence>("daily");
  const [date, setDate] = useState("2026-08-13");
  const [time, setTime] = useState("09:00");
  const [weekday, setWeekday] = useState("4");
  const [tickStatus, setTickStatus] = useState("");

  const schedulePreview = useMemo(() => {
    if (recurrence === "one_time") {
      return `将在 ${date} ${time} 执行一次`;
    }
    if (recurrence === "weekly") {
      const weekdayLabel = WEEKDAYS.find((item) => item.value === weekday)?.label ?? "周四";
      return `将在每${weekdayLabel} ${time} 执行`;
    }
    return `将在每天 ${time} 执行`;
  }, [date, recurrence, time, weekday]);

  const createSchedule = useMutation({
    mutationFn: (payload: ScheduleCreatePayload) => api.createSchedule(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });

  const tickSchedules = useMutation({
    mutationFn: () => api.tickSchedules(localDateTimeWithShanghaiOffset(date, time)),
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
    if (!trimmedName || !trimmedMessage || !date || !time) return;

    const basePayload = {
      name: trimmedName,
      message: trimmedMessage,
      mode: "dispatch" as const,
      workflow_id: "scheduled_task",
      timezone: "Asia/Shanghai",
      misfire_policy: "fire_once" as const,
      budget: 16384,
      metadata: { openclaw: "windows_desktop" },
    };
    const payload: ScheduleCreatePayload =
      recurrence === "one_time"
        ? {
            ...basePayload,
            kind: "one_time",
            run_at: localDateTimeWithShanghaiOffset(date, time),
          }
        : {
            ...basePayload,
            kind: "cron",
            cron: cronFromTime(time, recurrence === "weekly" ? weekday : undefined),
          };
    createSchedule.mutate(payload);
  }

  return (
    <section>
      <p className="eyebrow">Scheduled operations</p>
      <h2>计划任务</h2>
      <p>把需要定时完成的工作按日程提交到普通任务通道，仍然遵守模型能力、队列、OpenClaw 审批和审计边界。</p>

      <form className="form-grid schedule-form" aria-label="计划任务表单" onSubmit={submit}>
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
        <label htmlFor="schedule-recurrence">
          重复类型
          <select
            id="schedule-recurrence"
            value={recurrence}
            onChange={(event) => setRecurrence(event.target.value as Recurrence)}
          >
            <option value="one_time">一次性</option>
            <option value="daily">每天</option>
            <option value="weekly">每周</option>
          </select>
        </label>
        {recurrence === "one_time" ? (
          <label htmlFor="schedule-date">
            执行日期
            <input id="schedule-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
          </label>
        ) : null}
        {recurrence === "weekly" ? (
          <label htmlFor="schedule-weekday">
            星期
            <select id="schedule-weekday" value={weekday} onChange={(event) => setWeekday(event.target.value)}>
              {WEEKDAYS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label htmlFor="schedule-time">
          执行时间
          <input id="schedule-time" type="time" value={time} onChange={(event) => setTime(event.target.value)} />
        </label>
        <p className="schedule-preview" role="status">
          {schedulePreview}
        </p>
        <div className="toolbar">
          <button type="submit" disabled={createSchedule.isPending}>
            {createSchedule.isPending ? "保存中..." : "保存计划任务"}
          </button>
          <button
            type="button"
            className="secondary-action"
            disabled={tickSchedules.isPending}
            onClick={() => tickSchedules.mutate()}
          >
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
              <dt>频率</dt>
              <dd>{describeSchedule(schedule)}</dd>
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
                if (window.confirm(`删除计划任务 ${schedule.name}?`)) {
                  deleteSchedule.mutate(schedule.id);
                }
              }}
            >
              删除计划任务
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}
