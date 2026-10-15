import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api, ApiError, type CurrentUser, type PluginHandoverCreateInput, type PluginHandoverReceipt,
  type RunDetail, type WorkspaceFileList } from "../api/client";
import "./PluginHandoverPanel.css";

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const HANDOVER_ID = /^gh-[a-f0-9]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
type WorkspaceScope = { projectId: string; sessionId: string };
type WorkspaceListing = { scope: string; status: "loading" | "ready" | "error"; paths: string[] };
const STATUS_LABELS: Record<string, string> = {
  planned: "计划已创建", awaiting_signature: "等待签名归档", pending: "待审批",
  cancelled: "已取消", failed: "计划失败",
};
type CreateAttempt = {
  input: PluginHandoverCreateInput;
  key: string;
};
type ConfirmAttempt = {
  input: { expected_version: number; plan_hash: string; key_id: string };
  archive: File;
  key: string;
};

function operationKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return "handover-" + Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function localExpiry() {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function permitted(user: CurrentUser | null, permission: string) {
  return Boolean(user?.permissions.some((entry) => entry === "*" || entry === permission
    || entry === permission.split(":")[0] + ":*"));
}

function safeError(error: unknown, action: string) {
  if (error instanceof ApiError) {
    if (error.status === 403) return `${action}失败：当前账号权限不足。`;
    if (error.status === 404) return `${action}失败：计划不存在或当前账号不可见。`;
    if (error.status === 409) return `${action}失败：计划状态已变化，请刷新回执。`;
    if (error.status === 413) return `${action}失败：归档过大。`;
    if (error.status === 422) return `${action}失败：请求或签名归档不符合要求。`;
    if (error.status === 503) return `${action}失败：服务暂不可用。结果未确认，请刷新回执核实。`;
  }
  return `${action}结果未确认，请刷新回执核实。`;
}

function safeWorkspacePath(path: string) {
  return typeof path === "string" && path.length > 0 && Array.from(path).length <= 255
    && !/[\\:%\x00-\x1f\x7f<>"|?*]/.test(path)
    && path.split("/").every((part) => part !== "" && part !== "." && part !== ".."
      && part === part.trim() && !part.endsWith(".")
      && !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(part));
}

function checkedWorkspacePaths(list: WorkspaceFileList, scope: WorkspaceScope) {
  const base = `/api/v1/workspaces/projects/${scope.projectId}/sessions/${scope.sessionId}`;
  if (list.bundle_download_url !== `${base}/bundle/download`) throw new Error("workspace scope mismatch");
  const seen = new Set<string>();
  const paths = list.items.map((item) => {
    if (!safeWorkspacePath(item.path)) throw new Error("unsafe workspace path");
    // Conservatively reject aliases that would collide on supported server platforms.
    const normalized = item.path.normalize("NFKC").toLowerCase().replace(/ß/g, "ss").replace(/ς/g, "σ");
    const encoded = encodeURIComponent(item.path).replace(/[!'()*]/g,
      (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
    if (seen.has(normalized) || item.download_url !== `${base}/files/download?path=${encoded}`) {
      throw new Error("workspace path identity mismatch");
    }
    seen.add(normalized);
    return item.path;
  });
  if (Array.from(seen).some((path) => path.split("/").slice(0, -1)
    .some((_, index, parts) => seen.has(parts.slice(0, index + 1).join("/"))))) {
    throw new Error("workspace file conflicts with directory");
  }
  return paths;
}

export function PluginHandoverPanel({ runId, artifacts, workspaceScope }: {
  runId: string; artifacts: RunDetail["artifacts"]; workspaceScope?: WorkspaceScope;
}) {
  const [open, setOpen] = useState(false);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [authError, setAuthError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const sources = artifacts.filter((artifact) => UUID.test(runId) && UUID.test(artifact.id)
    && artifact.download_url === `/api/v1/runs/${runId}/artifacts/${artifact.id}/download`
    && (artifact.mime_type === "application/zip" || /\.zip$/i.test(artifact.filename || artifact.title)));
  const [artifactId, setArtifactId] = useState("");
  const selectedArtifact = artifactId || sources[0]?.id || "";
  const [expiry, setExpiry] = useState(localExpiry);
  const [lookup, setLookup] = useState("");
  const [receipt, setReceipt] = useState<PluginHandoverReceipt | null>(null);
  const createAttempt = useRef<CreateAttempt | null>(null);
  const [sourceMode, setSourceMode] = useState<"artifact" | "workspace">("artifact");
  const [workspace, setWorkspace] = useState<WorkspaceListing | null>(null);
  const [selection, setSelection] = useState<{ scope: string; paths: string[] }>({ scope: "", paths: [] });
  const [listingRetry, setListingRetry] = useState(0);
  const listingEpoch = useRef(0);
  const confirmAttempts = useRef(new Map<string, ConfirmAttempt>());
  const [uncertain, setUncertain] = useState(new Set<string>());
  const [keyId, setKeyId] = useState("");
  const [archive, setArchive] = useState<File | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const close = useCallback(() => {
    listingEpoch.current += 1;
    setOpen(false);
    setUser(null);
    setWorkspace(null);
    setSelection({ scope: "", paths: [] });
    setArchive(null);
    setKeyId("");
    setAcknowledged(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setUser(null);
    setAuthError("");
    void api.me().then((value) => { if (active) setUser(value); })
      .catch(() => { if (active) setAuthError("账号权限读取失败，写操作已禁用。"); });
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
      }
      if (event.key !== "Tab") return;
      const elements = Array.from(dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]',
      ) ?? []);
      const first = elements[0], last = elements[elements.length - 1];
      const outside = !dialog.current?.contains(document.activeElement) || document.activeElement === dialog.current;
      if (outside || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      active = false;
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [open, close]);

  const canRead = permitted(user, "run:read") && permitted(user, "plugin:read");
  const canCreate = canRead && permitted(user, "run:create");
  const canInstall = canRead && permitted(user, "plugin:write");
  const projectId = workspaceScope?.projectId;
  const sessionId = workspaceScope?.sessionId;
  const validScope = Boolean(projectId && sessionId && projectId === projectId.trim() && sessionId === sessionId.trim()
    && IDENTIFIER.test(projectId) && IDENTIFIER.test(sessionId));
  const scopeKey = validScope ? JSON.stringify([runId, projectId, sessionId]) : "";
  const frozenCreate = createAttempt.current;
  const mode = frozenCreate?.input.source.kind ?? sourceMode;
  const frozenWorkspace = frozenCreate?.input.source.kind === "workspace" ? frozenCreate.input.source : null;
  const currentListing = workspace?.scope === scopeKey ? workspace : null;
  const selectedPaths = selection.scope === scopeKey ? selection.paths : [];
  const filePaths = frozenWorkspace?.paths ?? (currentListing?.status === "ready" ? currentListing.paths : []);
  const sourceLocked = busy || Boolean(frozenCreate) || Boolean(receipt);
  const validSelection = mode === "artifact" ? sources.some((source) => source.id === selectedArtifact)
    : validScope && currentListing?.status === "ready" && selectedPaths.length > 0 && selectedPaths.length <= 200
      && selectedPaths.every((path) => currentListing.paths.includes(path));

  useEffect(() => {
    if (!open || mode !== "workspace" || !canCreate || !UUID.test(runId) || !validScope
      || frozenCreate || receipt || !projectId || !sessionId) return;
    const epoch = ++listingEpoch.current;
    setWorkspace({ scope: scopeKey, status: "loading", paths: [] });
    setSelection({ scope: scopeKey, paths: [] });
    void api.workspaceFiles(projectId, sessionId).then((value) => {
      if (epoch !== listingEpoch.current) return;
      const paths = checkedWorkspacePaths(value, { projectId, sessionId });
      setWorkspace({ scope: scopeKey, status: "ready", paths });
    }).catch(() => {
      if (epoch === listingEpoch.current) setWorkspace({ scope: scopeKey, status: "error", paths: [] });
    });
    return () => { listingEpoch.current += 1; };
  }, [open, mode, canCreate, runId, validScope, projectId, sessionId, scopeKey, listingRetry, frozenCreate, receipt]);

  function changeSource(next: "artifact" | "workspace") {
    if (sourceLocked || next === mode) return;
    listingEpoch.current += 1;
    setWorkspace(null);
    setSelection({ scope: "", paths: [] });
    setSourceMode(next);
  }
  const mutable = Boolean(receipt && (receipt.status === "planned" || receipt.status === "awaiting_signature")
    && Date.parse(receipt.expires_at) > Date.now() && !uncertain.has(receipt.handover_id));
  const frozenConfirm = receipt ? confirmAttempts.current.get(receipt.handover_id) : undefined;
  const validArchive = Boolean(archive && /\.zip$/i.test(archive.name) && archive.size > 0 && archive.size <= 2_000_000);
  const canConfirm = canInstall && mutable && (Boolean(frozenConfirm) || (Boolean(keyId.trim()) && validArchive && acknowledged));
  const canCancel = canCreate && mutable && (receipt?.creator_id === user?.user_id || canInstall);

  function accept(value: PluginHandoverReceipt, expectedId?: string) {
    if (value.run_id !== runId || !HANDOVER_ID.test(value.handover_id)
      || (expectedId && value.handover_id !== expectedId)) throw new Error("receipt identity mismatch");
    if (value.handover_id !== receipt?.handover_id) {
      setKeyId("");
      setArchive(null);
      setAcknowledged(false);
      if (fileInput.current) fileInput.current.value = "";
    }
    setReceipt(value);
    setLookup(value.handover_id);
  }

  async function perform(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try { await action(); }
    finally { lock.current = false; setBusy(false); }
  }

  async function create() {
    if (!canCreate || receipt || !UUID.test(runId) || (!createAttempt.current && !validSelection)) return;
    await perform(async () => {
      try {
        if (!createAttempt.current) {
          const expires = new Date(expiry);
          if (!Number.isFinite(expires.getTime()) || expires.getTime() <= Date.now()) {
            setError("请选择未来的计划到期时间。");
            return;
          }
          createAttempt.current = {
            input: { source: mode === "workspace" && projectId && sessionId
              ? { kind: "workspace", project_id: projectId, session_id: sessionId, paths: [...selectedPaths] }
              : { kind: "artifact", artifact_id: selectedArtifact }, expires_at: expires.toISOString() },
            key: operationKey(),
          };
        }
        const attempt = createAttempt.current;
        accept(await api.createPluginHandover(runId, attempt.input, attempt.key));
      } catch (caught) { setError(safeError(caught, "创建计划")); }
    });
  }

  async function get(handoverId: string) {
    if (!UUID.test(runId) || !HANDOVER_ID.test(handoverId)) return;
    await perform(async () => {
      try {
        accept(await api.pluginHandover(runId, handoverId), handoverId);
        setUncertain((current) => { const next = new Set(current); next.delete(handoverId); return next; });
      } catch (caught) { setError(safeError(caught, "载入回执")); }
    });
  }

  async function cancel() {
    if (!receipt || !canCancel) return;
    const current = receipt;
    await perform(async () => {
      try {
        accept(await api.cancelPluginHandover(runId, current.handover_id, {
          expected_version: current.version, plan_hash: current.plan_hash,
        }), current.handover_id);
      } catch (caught) {
        setUncertain((ids) => new Set(ids).add(current.handover_id));
        setError(safeError(caught, "取消计划"));
      }
    });
  }

  async function confirm() {
    if (!receipt || !canConfirm) return;
    const current = receipt;
    await perform(async () => {
      try {
        let attempt = confirmAttempts.current.get(current.handover_id);
        if (!attempt) {
          if (!archive || !acknowledged) return;
          attempt = { input: { expected_version: current.version, plan_hash: current.plan_hash, key_id: keyId.trim() },
            archive, key: operationKey() };
          confirmAttempts.current.set(current.handover_id, attempt);
        }
        accept(await api.confirmPluginHandover(runId, current.handover_id, attempt.input, attempt.archive, attempt.key), current.handover_id);
      } catch (caught) {
        setUncertain((ids) => new Set(ids).add(current.handover_id));
        setError(safeError(caught, "确认提交"));
      }
    });
  }

  const controlsDisabled = busy || !canInstall || !mutable || Boolean(frozenConfirm);
  return <>
    <div className="plugin-handover-entry">
      <button type="button" aria-expanded={open} onClick={() => setOpen(true)}>插件安装计划</button>
    </div>
    {open ? createPortal(
      <div className="plugin-handover-backdrop" data-testid="plugin-handover-backdrop"
        onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
        <div className="plugin-handover-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}
          tabIndex={-1} ref={dialog}>
          <header className="plugin-handover-header">
            <h2 id={titleId}>插件安装计划</h2>
            <button type="button" className="plugin-handover-close" aria-label="关闭安装计划" title="关闭安装计划"
              onClick={close}>×</button>
          </header>
          <div className="plugin-handover-content" aria-busy={busy}>
            <p className="plugin-handover-authority">{user ? `当前账号：${user.username || user.role}` : "正在读取账号权限…"}</p>
            {authError ? <p role="alert">{authError}</p> : null}
            {error ? <p role="alert">{error}</p> : null}
            <section className="plugin-handover-section" aria-label="源产物">
              <h3>源产物</h3>
              <div className="plugin-handover-source-modes" role="radiogroup" aria-label="计划来源">
                <label><input type="radio" name={`${titleId}-source`} checked={mode === "artifact"}
                  disabled={sourceLocked} onChange={() => changeSource("artifact")} />ZIP 产物</label>
                <label><input type="radio" name={`${titleId}-source`} checked={mode === "workspace"}
                  disabled={sourceLocked} onChange={() => changeSource("workspace")} />工作区文件</label>
              </div>
              {mode === "artifact" ? (sources.length ? <>
                <label>ZIP 产物<select value={selectedArtifact} disabled={busy || Boolean(createAttempt.current) || Boolean(receipt)}
                  onChange={(event) => setArtifactId(event.target.value)}>
                  {sources.map((source) => <option key={source.id} value={source.id}>{source.filename || source.title}</option>)}
                </select></label>
              </> : <p>此运行暂无可用于计划的 ZIP 产物</p>) : <>
                {frozenWorkspace ? <p>项目：{frozenWorkspace.project_id} · 会话：{frozenWorkspace.session_id}</p>
                  : !validScope || !UUID.test(runId) ? <p>当前运行没有可用的工作区范围，无法读取文件。</p>
                    : !canCreate ? <p>{user ? "当前账号无创建计划权限，无法读取工作区文件。" : "正在读取账号权限…"}</p>
                      : <>
                        <p>项目：{projectId} · 会话：{sessionId}</p>
                        {(!currentListing || currentListing.status === "loading") && !receipt
                          ? <p role="status">正在读取工作区文件…</p> : null}
                        {currentListing?.status === "error" ? <p role="alert">工作区文件读取失败或清单不符合安全要求。</p> : null}
                        {currentListing?.status === "ready" && !filePaths.length
                          ? <p>此工作区暂无可用于计划的文件</p> : null}
                        {currentListing && currentListing.status !== "loading" ? <button type="button" disabled={sourceLocked}
                          onClick={() => setListingRetry((value) => value + 1)}>重试读取文件</button> : null}
                      </>}
                {filePaths.length ? <div className="plugin-handover-files" role="group" aria-label="待冻结文件">
                  {filePaths.map((path) => {
                    const checked = Boolean(frozenWorkspace) || selectedPaths.includes(path);
                    return <label key={path} className="plugin-handover-checkbox"><input type="checkbox" checked={checked}
                      disabled={sourceLocked || !canCreate || (!checked && selectedPaths.length >= 200)}
                      onChange={(event) => {
                        if (sourceLocked || !canCreate || currentListing?.status !== "ready") return;
                        const paths = event.target.checked ? [...selectedPaths, path] : selectedPaths.filter((item) => item !== path);
                        if (paths.length <= 200) setSelection({ scope: scopeKey, paths });
                      }} /><span>{path}</span></label>;
                  })}
                  <p>已选择 {frozenWorkspace?.paths.length ?? selectedPaths.length} / 200</p>
                </div> : null}
              </>}
              {(mode === "workspace" || sources.length > 0) ? <>
                <label>计划到期时间<input type="datetime-local" value={expiry} disabled={busy || Boolean(createAttempt.current) || Boolean(receipt)}
                  onChange={(event) => setExpiry(event.target.value)} /></label>
                <button type="button" disabled={busy || !canCreate || Boolean(receipt) || !UUID.test(runId)
                  || (!frozenCreate && !validSelection)} onClick={() => void create()}>
                  {createAttempt.current && !receipt ? "重试创建原计划" : "创建安装计划"}
                </button>
              </> : null}
            </section>
            <section className="plugin-handover-section" aria-label="查阅计划">
              <h3>查阅计划</h3>
              <label>计划 ID<input value={lookup} maxLength={67} placeholder="gh-…" disabled={busy}
                onChange={(event) => setLookup(event.target.value)} autoComplete="off" spellCheck={false} /></label>
              <div className="plugin-handover-actions">
                <button type="button" disabled={busy || !UUID.test(runId) || !HANDOVER_ID.test(lookup)} onClick={() => void get(lookup)}>载入回执</button>
                {receipt ? <button type="button" disabled={busy} onClick={() => void get(receipt.handover_id)}>刷新当前回执</button> : null}
              </div>
            </section>
            {receipt ? <>
              <section className="plugin-handover-section" aria-label="安装计划回执">
                <h3>安装计划回执</h3>
                <p className="plugin-handover-status">{STATUS_LABELS[receipt.status] || "未知状态，请刷新回执核实"}</p>
                {Date.parse(receipt.expires_at) <= Date.now() ? <p>计划已到期，仅可查阅回执。</p> : null}
                {receipt.status === "pending" ? <p>待审批，插件仍禁用，尚不可调用。</p> : null}
                <dl>
                  <dt>计划 ID</dt><dd>{receipt.handover_id}</dd>
                  <dt>插件 ID</dt><dd>{receipt.plugin_id}</dd>
                  <dt>插件版本</dt><dd>{receipt.plugin_version}</dd>
                  <dt>回执版本</dt><dd>{receipt.version}</dd>
                  <dt>权限</dt><dd>{receipt.permissions.length ? receipt.permissions.join("、") : "无声明权限"}</dd>
                  <dt>风险</dt><dd>{receipt.risk_summary.length ? receipt.risk_summary.join("；") : "未声明风险"}</dd>
                  <dt>计划 hash</dt><dd><code>{receipt.plan_hash}</code></dd>
                  <dt>到期时间</dt><dd>{new Date(receipt.expires_at).toLocaleString("zh-CN")}</dd>
                </dl>
                <button type="button" disabled={busy || !canCancel} onClick={() => void cancel()}>取消计划</button>
              </section>
              <section className="plugin-handover-section" aria-label="提交签名归档">
                <h3>提交签名归档</h3>
                {!canInstall ? <p>当前账号无插件写入权限，确认提交已禁用。</p> : null}
                <label>既有可信 key ID<input value={frozenConfirm?.input.key_id ?? keyId} disabled={controlsDisabled}
                  onChange={(event) => setKeyId(event.target.value)} maxLength={128} autoComplete="off" spellCheck={false} /></label>
                <label>已签名 ZIP<input type="file" accept=".zip,application/zip" disabled={controlsDisabled} ref={fileInput}
                  onChange={(event) => { setArchive(event.target.files?.[0] ?? null); setAcknowledged(false); }} /></label>
                {archive && !validArchive ? <p role="alert">请选择非空 ZIP，大小不超过 2,000,000 字节。</p> : null}
                {frozenConfirm ? <p>本次归档：{frozenConfirm.archive.name}</p> : null}
                <label className="plugin-handover-checkbox"><input type="checkbox" checked={Boolean(frozenConfirm) || acknowledged}
                  readOnly={Boolean(frozenConfirm)} disabled={controlsDisabled}
                  onChange={(event) => setAcknowledged(event.target.checked)} />
                  {frozenConfirm ? "已确认的原归档，仅登记待审批插件" : "确认提交此归档，仅登记待审批插件"}</label>
                <div className="plugin-handover-actions">
                  <button type="button" disabled={busy || !canConfirm} onClick={() => void confirm()}>
                    {frozenConfirm && (receipt.status === "planned" || receipt.status === "awaiting_signature") ? "重试原提交" : "提交待审批"}
                  </button>
                  {receipt.status === "pending" ? <a href="/mcp">前往插件审批</a> : null}
                </div>
              </section>
            </> : null}
          </div>
        </div>
      </div>, document.body,
    ) : null}
  </>;
}
