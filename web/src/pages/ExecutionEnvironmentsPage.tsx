import { useQuery } from "@tanstack/react-query";

import { api, formatApiError, type ExecutionBackend } from "../api/client";

const REASON_TEXT: Record<string, string> = {
  systemd_tools_not_found: "服务器未安装 systemd-run 或 systemctl。",
  systemd_broker_unavailable: "systemd 安全执行代理未启动，或 Agent Hub 服务无权连接。",
  systemd_broker_invalid_response: "systemd 安全执行代理返回了无效响应。",
  systemd_transient_unit_unavailable: "服务账号无法创建 systemd 临时隔离单元。",
  docker_cli_not_found: "服务器未安装 Docker CLI。",
  docker_daemon_unavailable: "Docker 服务不可访问。",
  docker_runner_image_not_found: "缺少 agent-hub-skill-runner:latest 运行镜像。",
  docker_runner_unavailable: "Docker 技能运行镜像无法按隔离策略启动。",
  ssh_not_configured: "尚未配置 SSH 主机、用户和 known_hosts 文件。",
  ssh_configuration_invalid: "SSH 配置不完整，或密钥、known_hosts 文件不可用。",
  ssh_health_check_failed: "无法连接 SSH 远端 runner，或远端隔离代理未就绪。",
  ssh_protocol_mismatch: "SSH 远端 runner 的协议版本不兼容。",
  modal_not_configured: "尚未配置 Modal 技能沙箱 HTTPS 端点。",
  modal_configuration_invalid: "Modal 技能沙箱端点配置无效。",
  modal_health_check_failed: "Modal 技能沙箱健康检查失败。",
  modal_protocol_mismatch: "Modal 技能沙箱协议版本不兼容。",
  daytona_not_configured: "尚未配置 Daytona 技能沙箱 HTTPS 端点。",
  daytona_configuration_invalid: "Daytona 技能沙箱端点配置无效。",
  daytona_health_check_failed: "Daytona 技能沙箱健康检查失败。",
  daytona_protocol_mismatch: "Daytona 技能沙箱协议版本不兼容。",
  vercel_not_configured: "尚未配置 Vercel Sandbox HTTPS 端点。",
  vercel_configuration_invalid: "Vercel Sandbox 端点配置无效。",
  vercel_health_check_failed: "Vercel Sandbox 健康检查失败。",
  vercel_protocol_mismatch: "Vercel Sandbox 协议版本不兼容。",
};

function backendStatusText(backend: ExecutionBackend) {
  if (backend.available) return "可用";
  return backend.reason ? REASON_TEXT[backend.reason] ?? `不可用：${backend.reason}` : "不可用";
}

export function ExecutionEnvironmentsPage() {
  const backends = useQuery({
    queryKey: ["execution-backends"],
    queryFn: () => api.executionBackends(),
  });

  if (backends.isLoading) return <p>正在探测执行环境...</p>;
  if (backends.isError) {
    return <p role="alert">{formatApiError(backends.error, "执行环境探测失败")}</p>;
  }

  const items = backends.data ?? [];
  const availableCount = items.filter((item) => item.available).length;

  return (
    <section>
      <p className="eyebrow">Execution environments</p>
      <h2>执行环境</h2>
      <p>这里显示服务端已经接入的真实技能执行器及当前可用状态。任务权限是访问上限，技能运行器始终采用更严格的固定隔离。</p>
      <div className="status-grid" aria-label="执行环境状态">
        <article className="status-card">
          <span>已接入</span>
          <p>{items.length} 个</p>
        </article>
        <article className="status-card">
          <span>当前可用</span>
          <p>{availableCount} 个</p>
        </article>
        <article className="status-card">
          <span>不可用</span>
          <p>{items.length - availableCount} 个</p>
        </article>
      </div>

      <div className="card-grid execution-environment-grid" aria-label="执行环境列表">
        {items.map((backend) => (
          <article className="mini-card execution-environment-card" key={backend.id}>
            <div className="execution-environment-heading">
              <div>
                <p className="eyebrow">{backend.adapter}</p>
                <h3>{backend.name}</h3>
              </div>
              <span className={`status-pill ${backend.available ? "success" : "danger"}`}>
                {backendStatusText(backend)}
              </span>
            </div>
            <p>{backend.description}</p>
            <dl className="detail-list">
              <div>
                <dt>隔离</dt>
                <dd>{backend.isolation}</dd>
              </div>
              <div>
                <dt>资源成本</dt>
                <dd>{backend.cost}</dd>
              </div>
              <div>
                <dt>任务权限上限</dt>
                <dd>{backend.supported_sandbox_profiles.join("、")}</dd>
              </div>
            </dl>
          </article>
        ))}
      </div>

      <button type="button" className="secondary-action" onClick={() => void backends.refetch()}>
        重新探测
      </button>
    </section>
  );
}
