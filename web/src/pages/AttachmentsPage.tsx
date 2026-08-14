import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, formatApiError, type AttachmentUpload } from "../api/client";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

type AttachmentSortKey = "filename" | "kind" | "size" | "expires" | "sha";

type AttachmentColumnFilters = {
  expires: string;
  filename: string;
  kind: string;
  sha: string;
  size: string;
};

const EMPTY_ATTACHMENT_FILTERS: AttachmentColumnFilters = {
  expires: "",
  filename: "",
  kind: "",
  sha: "",
  size: "",
};

function attachmentColumnValue(item: AttachmentUpload, key: AttachmentSortKey) {
  if (key === "filename") return `${item.filename} ${item.id}`;
  if (key === "kind") return `${item.kind} ${item.content_type}`;
  if (key === "size") return String(item.size_bytes).padStart(16, "0");
  if (key === "expires") return item.expires_at;
  return item.sha256;
}

function matchesAttachmentSearch(item: AttachmentUpload, query: string) {
  return textContains([item.id, item.filename, item.kind, item.content_type, item.size_bytes, item.sha256, item.expires_at].join(" "), query);
}

function matchesAttachmentColumns(item: AttachmentUpload, filters: AttachmentColumnFilters) {
  return (
    textContains(`${item.filename} ${item.id}`, filters.filename) &&
    textContains(`${item.kind} ${item.content_type}`, filters.kind) &&
    textContains(formatBytes(item.size_bytes), filters.size) &&
    textContains(item.expires_at, filters.expires) &&
    textContains(item.sha256, filters.sha)
  );
}

function sortedAttachments(items: AttachmentUpload[], sort: SortState<AttachmentSortKey>) {
  return [...items].sort((left, right) => compareText(attachmentColumnValue(left, sort.key), attachmentColumnValue(right, sort.key), sort.direction));
}

export function AttachmentsPage() {
  const queryClient = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [searchTerm, setSearchTerm] = useState("");
  const [columnFilters, setColumnFilters] = useState<AttachmentColumnFilters>(EMPTY_ATTACHMENT_FILTERS);
  const [sort, setSort] = useState<SortState<AttachmentSortKey>>({ key: "filename", direction: "asc" });
  const attachments = useQuery({ queryKey: ["attachments"], queryFn: () => api.attachments() });
  const deleteAttachment = useMutation({
    mutationFn: (id: string) => api.deleteAttachment(id),
    onSuccess: (_result, id) => {
      setSelectedIds((current) => current.filter((item) => item !== id));
      void queryClient.invalidateQueries({ queryKey: ["attachments"] });
    },
  });
  const bulkDeleteAttachments = useMutation({
    mutationFn: (ids: string[]) => api.bulkDeleteAttachments(ids),
    onSuccess: (_result, ids) => {
      setSelectedIds((current) => current.filter((item) => !ids.includes(item)));
      void queryClient.invalidateQueries({ queryKey: ["attachments"] });
    },
  });

  function updateColumnFilter(key: keyof AttachmentColumnFilters, value: string) {
    setColumnFilters((current) => ({ ...current, [key]: value }));
  }

  function confirmDelete(item: AttachmentUpload) {
    if (!window.confirm(`确定删除附件「${item.filename}」吗？删除后对话里不能再引用它。`)) return;
    deleteAttachment.mutate(item.id);
  }

  function toggleAttachment(id: string) {
    setSelectedIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  function toggleAllAttachments(ids: string[]) {
    setSelectedIds((current) => {
      const allSelected = ids.length > 0 && ids.every((id) => current.includes(id));
      if (allSelected) return current.filter((id) => !ids.includes(id));
      return Array.from(new Set([...current, ...ids]));
    });
  }

  function confirmBulkDelete(ids: string[]) {
    if (ids.length === 0) return;
    if (!window.confirm(`确认删除当前结果中已选的 ${ids.length} 个附件？删除后对话里不能再引用它们。`)) return;
    bulkDeleteAttachments.mutate(ids);
  }

  if (attachments.isLoading) return <p>正在加载附件...</p>;
  if (attachments.isError) {
    return <p role="alert">{formatApiError(attachments.error, "附件加载失败")}</p>;
  }

  const items = attachments.data ?? [];
  const filteredItems = items.filter((item) => matchesAttachmentSearch(item, searchTerm) && matchesAttachmentColumns(item, columnFilters));
  const visibleItems = sortedAttachments(filteredItems, sort);
  const itemIds = visibleItems.map((item) => item.id);
  const selectedItemIds = selectedIds.filter((id) => itemIds.includes(id));
  const allSelected = itemIds.length > 0 && itemIds.every((id) => selectedIds.includes(id));
  const busy = deleteAttachment.isPending || bulkDeleteAttachments.isPending;

  return (
    <section>
      <p className="eyebrow">Attachment storage</p>
      <h2>附件管理</h2>
      <p>这里管理从对话页上传的图片、文档和压缩包。删除会清理附件文件、元数据、压缩包 manifest 和已解压目录。</p>

      {deleteAttachment.isError ? <p role="alert">{formatApiError(deleteAttachment.error, "附件删除失败")}</p> : null}
      {bulkDeleteAttachments.isError ? (
        <p role="alert">{formatApiError(bulkDeleteAttachments.error, "附件批量删除失败")}</p>
      ) : null}

      {items.length === 0 ? (
        <article>
          <h3>还没有附件</h3>
          <p>从对话输入框上传文件后，会在这里显示并可删除。</p>
        </article>
      ) : (
        <>
          <div className="list-toolbar">
            <label>
              快速搜索附件
              <input
                type="search"
                aria-label="快速搜索附件"
                value={searchTerm}
                onChange={(event) => setSearchTerm(event.currentTarget.value)}
                placeholder="跨文件名、ID、类型、大小和校验值搜索"
              />
            </label>
            <button type="button" className="secondary-action" onClick={() => { setSearchTerm(""); setColumnFilters(EMPTY_ATTACHMENT_FILTERS); }}>
              清空筛选
            </button>
            <small>
              显示 {visibleItems.length} / {items.length}
            </small>
          </div>
          <div className="bulk-action-bar">
            <label className="inline-check compact-check">
              <input
                type="checkbox"
                aria-label="全选当前附件结果"
                checked={allSelected}
                disabled={itemIds.length === 0 || busy}
                onChange={() => toggleAllAttachments(itemIds)}
              />
              全选当前结果
            </label>
            <button
              type="button"
              className="danger-button"
              disabled={selectedItemIds.length === 0 || busy}
              onClick={() => confirmBulkDelete(selectedItemIds)}
            >
              {bulkDeleteAttachments.isPending ? "删除中..." : `批量删除已选附件（${selectedItemIds.length}）`}
            </button>
            <small>当前结果已选 {selectedItemIds.length}</small>
          </div>
          {visibleItems.length === 0 ? (
            <article>
              <h3>没有匹配附件</h3>
              <p>调整列筛选或清空筛选查看全部附件。</p>
            </article>
          ) : (
            <div className="table-shell">
              <table aria-label="已上传附件">
                <thead>
                  <tr>
                    <th>选择</th>
                    <th><SortHeader column="filename" label="文件" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>文件</SortHeader></th>
                    <th><SortHeader column="kind" label="类型" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>类型</SortHeader></th>
                    <th><SortHeader column="size" label="大小" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>大小</SortHeader></th>
                    <th><SortHeader column="expires" label="过期时间" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>过期时间</SortHeader></th>
                    <th><SortHeader column="sha" label="校验" sort={sort} onSort={(column) => setSort((current) => nextSortState(current, column))}>校验</SortHeader></th>
                    <th>操作</th>
                  </tr>
                  <tr className="table-filter-row">
                    <th></th>
                    <th><input aria-label="按附件文件筛选" value={columnFilters.filename} onChange={(event) => updateColumnFilter("filename", event.currentTarget.value)} placeholder="文件名或 ID" /></th>
                    <th><input aria-label="按附件类型筛选" value={columnFilters.kind} onChange={(event) => updateColumnFilter("kind", event.currentTarget.value)} placeholder="类型" /></th>
                    <th><input aria-label="按附件大小筛选" value={columnFilters.size} onChange={(event) => updateColumnFilter("size", event.currentTarget.value)} placeholder="例如 MB" /></th>
                    <th><input aria-label="按附件过期时间筛选" value={columnFilters.expires} onChange={(event) => updateColumnFilter("expires", event.currentTarget.value)} placeholder="时间" /></th>
                    <th><input aria-label="按附件校验筛选" value={columnFilters.sha} onChange={(event) => updateColumnFilter("sha", event.currentTarget.value)} placeholder="sha256" /></th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {visibleItems.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Select attachment ${item.filename}`}
                          checked={selectedIds.includes(item.id)}
                          disabled={busy}
                          onChange={() => toggleAttachment(item.id)}
                        />
                      </td>
                      <td>
                        <strong>{item.filename}</strong>
                        <p className="field-help">ID: {item.id}</p>
                      </td>
                      <td>{item.kind}</td>
                      <td>{formatBytes(item.size_bytes)}</td>
                      <td>{formatDate(item.expires_at)}</td>
                      <td>{item.sha256.slice(0, 12)}</td>
                      <td className="table-actions">
                        <button
                          type="button"
                          className="danger-action"
                          disabled={busy}
                          aria-label={`删除附件 ${item.filename}`}
                          onClick={() => confirmDelete(item)}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.ceil(value / 1024))} KB`;
}

function formatDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}