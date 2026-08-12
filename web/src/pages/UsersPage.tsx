import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError, type ManagedUser } from "../api/client";
import { useAuth } from "../auth/AuthProvider";

const ROLES = [
  {
    value: "super_admin",
    label: "超级管理员",
    description: "最高权限；可审计用户行为、保护初始管理员，并执行危险管理操作。",
  },
  {
    value: "admin",
    label: "管理员",
    description:
      "可配置模型、Agent、工作流、Skill、MCP 和插件；可安装能力，但不能绕过受保护账号限制。",
  },
  {
    value: "operator",
    label: "操作员",
    description: "可发起和控制任务，可使用已经启用的 Skill、MCP 与插件。",
  },
  {
    value: "viewer",
    label: "只读用户",
    description: "可查看任务与配置，可使用已启用能力，但不能修改系统配置。",
  },
] as const;

function roleLabel(role: string) {
  return ROLES.find((item) => item.value === role)?.label ?? role;
}

function can(permission: string, permissions: string[]) {
  if (permissions.includes("*") || permissions.includes(permission)) return true;
  const [namespace] = permission.split(":");
  return permissions.includes(`${namespace}:*`);
}

export function UsersPage() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("operator");
  const [message, setMessage] = useState<string | null>(null);
  const [resetTarget, setResetTarget] = useState<ManagedUser | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const permissions = auth.user?.permissions ?? [];
  const canWriteUsers = can("user:write", permissions);
  const currentUserId = auth.user?.user_id ?? "";

  const users = useQuery({ queryKey: ["users"], queryFn: () => api.users() });
  const refreshUsers = () => void queryClient.invalidateQueries({ queryKey: ["users"] });
  const createUser = useMutation({
    mutationFn: () => api.createUser({ username: username.trim(), password, role }),
    onSuccess: (user) => {
      setUsername("");
      setPassword("");
      setRole("operator");
      setMessage(`已创建用户 ${user.username}`);
      refreshUsers();
    },
  });
  const changeRole = useMutation({
    mutationFn: ({ userId, nextRole }: { userId: string; nextRole: string }) =>
      api.changeUserRole(userId, nextRole),
    onSuccess: (user) => {
      setMessage(`已将 ${user.username} 调整为 ${roleLabel(user.role)}`);
      refreshUsers();
    },
  });
  const setDisabled = useMutation({
    mutationFn: ({ userId, disabled }: { userId: string; disabled: boolean }) =>
      api.setUserDisabled(userId, disabled),
    onSuccess: (user) => {
      setMessage(`${user.username} 已${user.disabled ? "禁用" : "启用"}`);
      refreshUsers();
    },
  });
  const deleteUser = useMutation({
    mutationFn: (user: ManagedUser) => api.deleteUser(user.id).then(() => user),
    onSuccess: (user) => {
      setMessage(`已删除用户 ${user.username}`);
      refreshUsers();
    },
  });
  const resetUserPassword = useMutation({
    mutationFn: ({ userId, nextPassword }: { userId: string; nextPassword: string }) =>
      api.resetUserPassword(userId, nextPassword),
    onSuccess: (user) => {
      setResetTarget(null);
      setResetPassword("");
      setMessage(`已重置 ${user.username} 的密码`);
      refreshUsers();
    },
  });

  const operationError =
    createUser.error ??
    changeRole.error ??
    setDisabled.error ??
    resetUserPassword.error ??
    deleteUser.error;

  if (users.isLoading) return <p>正在加载用户...</p>;
  if (users.isError) {
    return <p role="alert">{formatApiError(users.error, "用户列表加载失败")}</p>;
  }

  return (
    <section>
      <p className="eyebrow">Access control</p>
      <h2>用户管理</h2>
      <p>
        管理控制台账号、角色和登录状态。初始超级管理员会被保护，不能被降级、禁用或删除。
        超级管理员可在“日志 → 审计日志”中查看用户管理行为。
      </p>

      <article>
        <h3>权限说明</h3>
        <div className="card-grid compact">
          {ROLES.map((item) => (
            <div key={item.value} className="mini-card">
              <strong>{item.label}</strong>
              <p>{item.description}</p>
            </div>
          ))}
        </div>
      </article>

      {canWriteUsers ? (
        <article>
          <h3>新增用户</h3>
          <form
            className="form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              setMessage(null);
              createUser.mutate();
            }}
          >
            <label htmlFor="new-username">
              用户名
              <input
                id="new-username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="ops-user"
                autoComplete="username"
                required
              />
              <small>小写字母开头，3-64 位，可用数字、_、-。</small>
            </label>
            <label htmlFor="new-password">
              初始密码
              <input
                id="new-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="至少 12 位"
                autoComplete="new-password"
                required
              />
              <small>保存时会使用 Argon2id 哈希，不会明文存储。</small>
            </label>
            <label htmlFor="new-role">
              角色
              <select id="new-role" value={role} onChange={(event) => setRole(event.target.value)}>
                {ROLES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" disabled={createUser.isPending}>
              {createUser.isPending ? "创建中..." : "创建用户"}
            </button>
          </form>
        </article>
      ) : null}

      {message ? <p role="status">{message}</p> : null}
      {operationError ? <p role="alert">{formatApiError(operationError, "用户操作失败")}</p> : null}

      {resetTarget ? (
        <article>
          <h3>重置密码</h3>
          <p>
            正在为 <strong>{resetTarget.username}</strong> 设置新密码。保存后旧密码立即失效，密码明文不会写入页面或日志。
          </p>
          <form
            className="form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              setMessage(null);
              resetUserPassword.mutate({ userId: resetTarget.id, nextPassword: resetPassword });
            }}
          >
            <label htmlFor="reset-password">
              新密码
              <input
                id="reset-password"
                aria-label="新密码"
                type="password"
                value={resetPassword}
                onChange={(event) => setResetPassword(event.target.value)}
                placeholder="至少 12 位"
                autoComplete="new-password"
                required
                minLength={12}
              />
              <small>建议使用 16 位以上随机密码；保存后请通过安全渠道交给用户。</small>
            </label>
            <button type="submit" disabled={resetUserPassword.isPending}>
              {resetUserPassword.isPending ? "保存中..." : "保存新密码"}
            </button>
            <button
              type="button"
              onClick={() => {
                setResetTarget(null);
                setResetPassword("");
              }}
            >
              取消
            </button>
          </form>
        </article>
      ) : null}

      <article>
        <h3>用户列表</h3>
        <table>
          <thead>
            <tr>
              <th>用户名</th>
              <th>角色</th>
              <th>状态</th>
              <th>飞书绑定</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.data?.map((user) => {
              const isSelf = user.id === currentUserId;
              const locked = user.protected || isSelf || !canWriteUsers;
              return (
                <tr key={user.id}>
                  <td>
                    <strong>{user.username}</strong>
                    {user.protected ? <span className="status-chip">初始管理员</span> : null}
                    {isSelf ? <span className="status-chip">当前账号</span> : null}
                  </td>
                  <td>
                    <select
                      aria-label={`修改 ${user.username} 的角色`}
                      value={user.role}
                      disabled={locked}
                      onChange={(event) =>
                        changeRole.mutate({ userId: user.id, nextRole: event.currentTarget.value })
                      }
                    >
                      {ROLES.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>{user.disabled ? "已禁用" : "正常"}</td>
                  <td>{user.feishu_open_id ?? "未绑定"}</td>
                  <td className="table-actions">
                    <button
                      type="button"
                      disabled={locked}
                      onClick={() => {
                        setMessage(null);
                        setResetPassword("");
                        setResetTarget(user);
                      }}
                    >
                      重置密码
                    </button>
                    <button
                      type="button"
                      disabled={locked}
                      onClick={() => setDisabled.mutate({ userId: user.id, disabled: !user.disabled })}
                    >
                      {user.disabled ? "启用" : "禁用"}
                    </button>
                    <button
                      type="button"
                      className="danger-button"
                      disabled={locked}
                      onClick={() => {
                        if (window.confirm(`确认删除用户 ${user.username}？此操作不可恢复。`)) {
                          deleteUser.mutate(user);
                        }
                      }}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </article>
    </section>
  );
}
