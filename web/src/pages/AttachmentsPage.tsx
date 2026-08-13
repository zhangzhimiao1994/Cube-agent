import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError, type AttachmentUpload } from "../api/client";

export function AttachmentsPage() {
  const queryClient = useQueryClient();
  const attachments = useQuery({ queryKey: ["attachments"], queryFn: () => api.attachments() });
  const deleteAttachment = useMutation({
    mutationFn: (id: string) => api.deleteAttachment(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["attachments"] }),
  });

  function confirmDelete(item: AttachmentUpload) {
    if (!window.confirm(`确定删除附件「${item.filename}」吗？删除后对话里不能再引用它。`)) return;
    deleteAttachment.mutate(item.id);
  }

  if (attachments.isLoading) return <p>正在加载附件...</p>;
  if (attachments.isError) {
    return <p role="alert">{formatApiError(attachments.error, "附件加载失败")}</p>;
  }

  const items = attachments.data ?? [];

  return (
    <section>
      <p className="eyebrow">Attachment storage</p>
      <h2>附件管理</h2>
      <p>这里管理从对话页上传的图片、文档和压缩包。删除会清理附件文件、元数据、压缩包 manifest 和已解压目录。</p>

      {deleteAttachment.isError ? <p role="alert">{formatApiError(deleteAttachment.error, "附件删除失败")}</p> : null}

      {items.length === 0 ? (
        <article>
          <h3>还没有附件</h3>
          <p>从对话输入框上传文件后，会在这里显示并可删除。</p>
        </article>
      ) : (
        <div className="table-shell">
          <table aria-label="已上传附件">
            <thead>
              <tr>
                <th>文件</th>
                <th>类型</th>
                <th>大小</th>
                <th>过期时间</th>
                <th>校验</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
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
                      disabled={deleteAttachment.isPending}
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
