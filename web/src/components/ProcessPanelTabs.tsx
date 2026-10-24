import { type ReactNode, useId, useRef, useState } from "react";

export function ProcessPanelTabs({ summary, records }: { summary: ReactNode; records: ReactNode }) {
  const [selected, setSelected] = useState(0);
  const id = useId();
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const labels = ["摘要", "真实调度记录"];
  return <>
    <div className="process-panel-tabs" role="tablist" aria-label="调度席视图">
      {labels.map((label, index) => <button key={label} ref={(button) => { buttons.current[index] = button; }}
        type="button" role="tab" id={`${id}-tab-${index}`} aria-controls={`${id}-panel-${index}`}
        aria-selected={selected === index} tabIndex={selected === index ? 0 : -1}
        onClick={() => setSelected(index)} onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
          setSelected(next);
          buttons.current[next]?.focus();
        }}>{label}</button>)}
    </div>
    {[summary, records].map((content, index) => <div key={index} className="process-panel-content" role="tabpanel"
      id={`${id}-panel-${index}`} aria-labelledby={`${id}-tab-${index}`} hidden={selected !== index} tabIndex={0}>
      {content}
    </div>)}
  </>;
}
