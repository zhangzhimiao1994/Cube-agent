import { type ReactNode, type Ref, useMemo } from "react";
import type { RunDetail } from "../api/client";
import { BoundedTextBlock, outputMachineLanguage } from "./OutputContent";

type RunEvent = RunDetail["events"][number];

const POSITIONS = { approve: "支持", reject: "反对", needs_user: "需要确认" } as const;
const SECTIONS = [
  ["claims", "观点"],
  ["evidence", "角色提供的依据"],
  ["objections", "分歧"],
  ["verification_needed", "待验证"],
] as const;

function publicRoleRecord(event: RunEvent) {
  if (event.kind !== "message.created" || !event.actor || !Number.isSafeInteger(event.sequence) || event.sequence < 1) {
    return null;
  }
  const value = event.payload.role_contribution;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (record.type !== "role_contribution" || record.schema_version !== 1 || record.status !== "proposed"
    || record.actor !== event.actor || record.source_sequence !== event.sequence) {
    return null;
  }
  return record;
}

function contribution(event: RunEvent) {
  const record = publicRoleRecord(event);
  if (!record || !event.actor) return null;
  const position = record.position;
  if (position !== "approve" && position !== "reject" && position !== "needs_user") return null;
  const sections = SECTIONS.flatMap(([key, label]) => {
    const entries = record[key];
    return Array.isArray(entries) && entries.length > 0 && entries.every((entry) => typeof entry === "string" && entry.trim())
      ? [{ key, label, entries: entries as string[] }] : [];
  });
  if (!sections.length) return null;
  return {
    event,
    actor: event.actor,
    model: typeof event.payload.logical_model === "string" ? event.payload.logical_model : "",
    position,
    sections,
  } as const;
}

export function discussionSourceEvent(events: readonly RunEvent[], sequence: number | null) {
  if (sequence === null) return null;
  const matches = events.filter((event) => event.sequence === sequence);
  return matches.length === 1 && contribution(matches[0]) ? matches[0] : null;
}

function contributions(mode: string | null | undefined, events: readonly RunEvent[]) {
  if (mode !== "discuss" && mode !== "hybrid") return [];
  const sequences = new Map<number, number>();
  for (const event of events) {
    sequences.set(event.sequence, (sequences.get(event.sequence) ?? 0) + 1);
  }
  return events
    .filter((event) => sequences.get(event.sequence) === 1)
    .map(contribution)
    .filter((row): row is NonNullable<typeof row> => row !== null)
    .sort((left, right) => left.event.sequence - right.event.sequence);
}

export function DiscussionMinutes({
  mode, events, selectedActor, actorName, onOpenSource, fallback = null,
}: {
  mode: string | null | undefined;
  events: readonly RunEvent[];
  selectedActor?: string | null;
  actorName?: (actor: string) => string;
  onOpenSource: (event: RunEvent) => void;
  fallback?: ReactNode;
}) {
  const all = useMemo(() => contributions(mode, events), [mode, events]);
  const actors = new Set(all.map((row) => row.actor));
  const rows = selectedActor ? all.filter((row) => row.actor === selectedActor) : all;
  if (actors.size < 2 || !rows.length) return fallback;
  return <section className="process-meeting-minutes process-public-excerpts" aria-label="会议纪要">
    <h4>会议纪要</h4>
    <p>{actors.size} 位角色留下了可引用的发言。角色意见和待验证事项来自下列公开记录。</p>
    {rows.map(({ event, actor, model, position, sections }) => <article key={event.sequence}>
      <header>
        <h5>{actorName?.(actor) || actor} · {POSITIONS[position]}</h5>
        {model ? <small>逻辑模型：{model}</small> : null}
      </header>
      {sections.map(({ key, label, entries }) => <div key={key}>
        <strong>{label}</strong>
        <ul>{entries.map((entry, index) => <li key={index}>{entry}</li>)}</ul>
      </div>)}
      <button type="button" className="secondary-action" onClick={() => onOpenSource(event)}>
        查看原记录 #{event.sequence}
      </button>
    </article>)}
  </section>;
}

export function DiscussionSourceRecord({ event, sourceRef }: { event: RunEvent; sourceRef?: Ref<HTMLElement> }) {
  const text = (event.message !== event.kind && event.message) || event.summary || "未提供公开正文";
  const language = outputMachineLanguage(text);
  const row = contribution(event);
  return <section className="process-record-expanded process-meeting-source" aria-label="纪要来源记录"
    ref={sourceRef} tabIndex={-1}>
    <h4>公开发言 #{event.sequence}</h4>
    <small>{event.actor}</small>
    {language ? <BoundedTextBlock text={text} language={language} label={language}
      shouldCollapse copyAriaLabel="复制公开发言" /> : <p>{text}</p>}
    {row ? <details>
      <summary>查看公开字段</summary>
      <BoundedTextBlock language="json" label="公开发言记录" shouldCollapse copyAriaLabel="复制公开发言记录"
        text={JSON.stringify({
          actor: row.actor, source_sequence: event.sequence, position: row.position,
          ...Object.fromEntries(row.sections.map(({ key, entries }) => [key, entries])),
        }, null, 2)} />
    </details> : null}
  </section>;
}
