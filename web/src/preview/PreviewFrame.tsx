import { useEffect, useRef } from "react";
import { api, type WebPreview } from "../api/client";
import { attachPreviewBridge } from "./transport";

export function PreviewFrame({ preview, title, onLoad }: { preview: WebPreview; title: string; onLoad?: () => void }) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const bridgeRef = useRef<ReturnType<typeof attachPreviewBridge> | null>(null);
  const identity = `${preview.id}:${preview.preview_url}`;
  const loadedRef = useRef<string | null>(null);
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame || !preview.application_transport || preview.status !== "ready") return;
    const bridge = attachPreviewBridge(frame, preview.id, (id, request, signal) => api.requestWebPreviewApplication(id, request, signal));
    bridgeRef.current = bridge;
    if (loadedRef.current === identity) bridge.frameLoaded();
    return () => { bridge.dispose(); if (bridgeRef.current === bridge) bridgeRef.current = null; };
  }, [preview.id, preview.status, preview.preview_url, preview.application_transport, identity]);
  return <iframe
    ref={frameRef}
    className="agent-workbench-web-preview"
    title={title}
    src={preview.preview_url ?? undefined}
    sandbox="allow-scripts allow-forms allow-modals"
    referrerPolicy="no-referrer"
    onLoad={() => { loadedRef.current = identity; bridgeRef.current?.frameLoaded(); onLoad?.(); }}
  />;
}
