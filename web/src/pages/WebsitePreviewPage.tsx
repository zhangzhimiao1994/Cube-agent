import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, formatApiError, type WebPreview } from "../api/client";
import { PreviewFrame } from "../preview/PreviewFrame";

export function WebsitePreviewPage() {
  const { previewId } = useParams();
  const [search] = useSearchParams();
  const conversationId = search.get("conversation") ?? "";
  const [preview, setPreview] = useState<WebPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const activeRef = useRef<WebPreview | null>(null);
  useEffect(() => { activeRef.current = preview; }, [preview]);
  useEffect(() => {
    let active = true;
    setPreview(null); setError(null);
    const load = async () => {
      if (!previewId || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(conversationId)) throw new Error("preview unavailable");
      const current = await api.webPreviewForConversation(conversationId);
      if (!current || current.id !== previewId || current.status !== "ready") throw new Error("preview unavailable");
      if (active) setPreview(current);
    };
    void load().catch(() => { if (active) setError("网站预览已停止或不可访问"); });
    return () => { active = false; };
  }, [previewId, conversationId]);
  useEffect(() => {
    if (!preview || preview.status !== "ready") return;
    let active = true;
    const interval = window.setInterval(() => {
      void api.renewWebPreview(preview.id).then((renewed) => {
        if (!active) return;
        setPreview(renewed.status === "ready" ? renewed : null);
      }).catch(() => { if (active) { setPreview(null); setError("网站预览已停止或不可访问"); } });
    }, 60_000);
    return () => { active = false; window.clearInterval(interval); };
  }, [preview?.id, preview?.status]);
  useEffect(() => {
    const close = (event: PageTransitionEvent) => {
      const current = activeRef.current;
      if (!event.persisted && current?.status === "ready") void api.stopWebPreview(current.id, { keepalive: true }).catch(() => undefined);
    };
    window.addEventListener("pagehide", close);
    return () => window.removeEventListener("pagehide", close);
  }, []);
  const stop = async () => {
    if (!preview) return;
    try { await api.stopWebPreview(preview.id); setPreview(null); setError("网站预览已停止"); }
    catch (caught) { setError(formatApiError(caught, "停止预览失败")); }
  };
  return <main className="website-preview-page">
    <header><strong>网站预览</strong>{preview ? <button type="button" className="secondary-action" onClick={() => void stop()}>停止预览</button> : null}</header>
    {error ? <p role="alert" className="form-error">{error}</p> : preview ? <PreviewFrame preview={preview} title="网站预览" /> : <p role="status">正在读取预览...</p>}
  </main>;
}
