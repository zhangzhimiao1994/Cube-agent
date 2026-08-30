import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError, type ManagedUser } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { compareText, nextSortState, SortHeader, textContains, type SortState } from "../components/TableTools";

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

type UserSortKey = "username" | "role" | "status" | "feishu";

type UserColumnFilters = {
  feishu: string;
  role: "all" | string;
  status: "all" | "enabled" | "disabled";
  username: string;
};

const EMPTY_USER_FILTERS: UserColumnFilters = {
  feishu: "",
  role: "all",
  status: "all",
  username: "",
};

function userStatus(user: ManagedUser) {
  return user.disabled ? "已禁用" : "正常";
}

function userSearchText(user: ManagedUser) {
  return [
    user.username,
    user.id,
    roleLabel(user.role),
    userStatus(user),
    user.feishu_open_id ?? "未绑定",
    user.protected ? "初始管理员" : "",
  ].join(" ");
}

function matchesUserColumns(user: ManagedUser, filters: UserColumnFilters) {
  return (
    textContains(`${user.username} ${user.id}`, filters.username) &&
    (filters.role === "all" || user.role === filters.role) &&
    (filters.status === "all" || (filters.status === "disabled") === user.disabled) &&
    textContains(user.feishu_open_id ?? "未绑定", filters.feishu)
  );
}

function sortedUsers(users: ManagedUser[], sort: SortState<UserSortKey>) {
  const copy = [...users];
  if (false) return copy;
  const direction = sort.direction === "asc" ? 1 : -1;
  return copy.sort((left, right) => {
    let result = 0;
    if (sort.key === "username") result = compareText(left.username, right.username, "asc");
    if (sort.key === "role") result = compareText(roleLabel(left.role), roleLabel(right.role), "asc");
    if (sort.key === "status") result = compareText(userStatus(left), userStatus(right), "asc");
    if (sort.key === "feishu") result = compareText(left.feishu_open_id ?? "未绑定", right.feishu_open_id ?? "未绑定", "asc");
    return sort.direction === "asc" ? result : -result;
  });
}

export function UsersPage() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("operator");
  const [message, setMessage] = useState<string | null>(null);
  const [editTarget, setEditTarget] = useState<ManagedUser | null>(null);
  const [editUsername, setEditUsername] = useState("");
  const [editRole, setEditRole] = useState("operator");
  const [editDisabled, setEditDisabled] = useState(false);
  const [resetTarget, setResetTarget] = useState<ManagedUser | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [feishuTarget, setFeishuTarget] = useState<ManagedUser | null>(null);
  const [feishuOpenId, setFeishuOpenId] = useState("");
  const [userSearchTerm, setUserSearchTerm] = useState("");
  const [userColumnFilters, setUserColumnFilters] = useState<UserColumnFilters>(EMPTY_USER_FILTERS);
  const [userSort, setUserSort] = useState<SortState<UserSortKey>>({ key: "username", direction: "asc" });
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
  const updateUser = useMutation({
    mutationFn: () => {
      if (!editTarget) throw new Error("未选择要编辑的用户");
      return api.updateUser(editTarget.id, {
        username: editUsername.trim(),
        role: editRole,
        disabled: editDisabled,
      });
    },
    onSuccess: (user) => {
      setEditTarget(null);
      setEditUsername("");
      setEditRole("operator");
      setEditDisabled(false);
      setMessage(`已更新用户 ${user.username}`);
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
  const bindFeishu = useMutation({
    mutationFn: () => {
      if (!feishuTarget) throw new Error("未选择要绑定飞书的用户");
      return api.bindUserFeishu(feishuTarget.id, feishuOpenId.trim());
    },
    onSuccess: (user) => {
      setFeishuTarget(null);
      setFeishuOpenId("");
      setMessage(`已绑定 ${user.username} 的飞书账号`);
      refreshUsers();
    },
  });
  const unbindFeishu = useMutation({
    mutationFn: (user: ManagedUser) => api.unbindUserFeishu(user.id),
    onSuccess: (user) => {
      setMessage(`已解绑 ${user.username} 的飞书账号`);
      refreshUsers();
    },
  });

  const operationError =
    createUser.error ??
    updateUser.error ??
    changeRole.error ??
    setDisabled.error ??
    resetUserPassword.error ??
    bindFeishu.error ??
    unbindFeishu.error ??
    deleteUser.error;

  if (users.isLoading) return <p>正在加载用户...</p>;
  if (users.isError) {
    return <p role="alert">{formatApiError(users.error, "用户列表加载失败")}</p>;
  }

  const userItems = users.data ?? [];
  const visibleUsers = sortedUsers(
    userItems.filter((user) => textContains(userSearchText(user), userSearchTerm) && matchesUserColumns(user, userColumnFilters)),
    userSort,
  );

  function updateUserColumnFilter<Key extends keyof UserColumnFilters>(key: Key, value: UserColumnFilters[Key]) {
    setUserColumnFilters((current) => ({ ...current, [key]: value }));
  }

  return (
    <section>
      <p className="eyebrow">Access control</p>
      <h2>用户管理</h2>
      <p>
        管理工作台账号、角色和登录状态。初始超级管理员会被保护，不能被降级、禁用或删除。
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

      {editTarget ? (
        <article>
          <h3>编辑用户</h3>
          <p>
            正在编辑 <strong>{editTarget.username}</strong>。用户名、角色和启用状态会一起保存；密码重置仍使用独立入口。
          </p>
          <form
            className="form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              setMessage(null);
              updateUser.mutate();
            }}
          >
            <label htmlFor="edit-username">
              用户名（编辑）
              <input
                id="edit-username"
                aria-label="用户名（编辑）"
                value={editUsername}
                onChange={(event) => setEditUsername(event.target.value)}
                autoComplete="username"
                required
              />
              <small>修改用户名会影响后续登录名；受保护的初始管理员不能改名。</small>
            </label>
            <label htmlFor="edit-role">
              角色（编辑）
              <select id="edit-role" aria-label="角色（编辑）" value={editRole} onChange={(event) => setEditRole(event.target.value)}>
                {ROLES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="inline-check compact-check" htmlFor="edit-disabled">
              <input
                id="edit-disabled"
                type="checkbox"
                checked={editDisabled}
                onChange={(event) => setEditDisabled(event.target.checked)}
              />
              禁用该用户
            </label>
            <div className="table-actions">
              <button type="submit" disabled={updateUser.isPending}>
                {updateUser.isPending ? "保存中..." : "保存修改"}
              </button>
              <button
                type="button"
                className="secondary-action"
                onClick={() => {
                  setEditTarget(null);
                  setEditUsername("");
                  setEditRole("operator");
                  setEditDisabled(false);
                }}
              >
                取消
              </button>
            </div>
          </form>
        </article>
      ) : null}

      {resetTarget ? (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="reset-password-title">
            <h3 id="reset-password-title">重置密码</h3>
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
                  autoFocus
                />
                <small>建议使用 16 位以上随机密码；保存后请通过安全渠道交给用户。</small>
              </label>
              <div className="table-actions">
                <button type="submit" disabled={resetUserPassword.isPending}>
                  {resetUserPassword.isPending ? "保存中..." : "保存新密码"}
                </button>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => {
                    setResetTarget(null);
                    setResetPassword("");
                  }}
                >
                  取消
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}

      {feishuTarget ? (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="bind-feishu-title">
            <h3 id="bind-feishu-title">绑定飞书账号</h3>
            <p>
              正在为 <strong>{feishuTarget.username}</strong> 绑定飞书 open_id。绑定后飞书消息会归属到这个管理台用户。
            </p>
            <form
              className="form-grid"
              onSubmit={(event) => {
                event.preventDefault();
                setMessage(null);
                bindFeishu.mutate();
              }}
            >
              <label htmlFor="feishu-open-id">
                飞书 open_id
                <input
                  id="feishu-open-id"
                  aria-label="飞书 open_id"
                  value={feishuOpenId}
                  onChange={(event) => setFeishuOpenId(event.target.value)}
                  placeholder="ou_xxx"
                  autoComplete="off"
                  required
                  autoFocus
                />
                <small>可从最近飞书任务的 channel_sender_external_id 中确认。</small>
              </label>
              <div className="table-actions">
                <button type="submit" disabled={bindFeishu.isPending}>
                  {bindFeishu.isPending ? "保存中..." : "保存飞书绑定"}
                </button>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => {
                    setFeishuTarget(null);
                    setFeishuOpenId("");
                  }}
                >
                  取消
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}

      <article>
        <h3>用户列表</h3>
        <div className="list-toolbar">
          <label>
            快速搜索用户
            <input
              type="search"
              aria-label="快速搜索用户"
              value={userSearchTerm}
              onChange={(event) => setUserSearchTerm(event.currentTarget.value)}
              placeholder="用户名、角色、状态或飞书 ID"
            />
          </label>
          <button type="button" className="secondary-action" onClick={() => { setUserSearchTerm(""); setUserColumnFilters(EMPTY_USER_FILTERS); }}>
            清空筛选
          </button>
        </div>
        {visibleUsers.length === 0 ? (
          <article>
            <h4>当前筛选没有匹配用户</h4>
            <p>调整列筛选或清空筛选查看全部用户。</p>
          </article>
        ) : (
          <table aria-label="用户列表">
            <thead>
              <tr>
                <th><SortHeader column="username" label="用户名" sort={userSort} onSort={(column) => setUserSort((current) => nextSortState(current, column))}>用户名</SortHeader></th>
                <th><SortHeader column="role" label="角色" sort={userSort} onSort={(column) => setUserSort((current) => nextSortState(current, column))}>角色</SortHeader></th>
                <th><SortHeader column="status" label="状态" sort={userSort} onSort={(column) => setUserSort((current) => nextSortState(current, column))}>状态</SortHeader></th>
                <th><SortHeader column="feishu" label="飞书绑定" sort={userSort} onSort={(column) => setUserSort((current) => nextSortState(current, column))}>飞书绑定</SortHeader></th>
                <th>操作</th>
              </tr>
              <tr className="table-filter-row">
                <th><input aria-label="按用户名筛选" value={userColumnFilters.username} onChange={(event) => updateUserColumnFilter("username", event.currentTarget.value)} placeholder="用户名或 ID" /></th>
                <th>
                  <select aria-label="按用户角色筛选" value={userColumnFilters.role} onChange={(event) => updateUserColumnFilter("role", event.currentTarget.value)}>
                    <option value="all">全部</option>
                    {ROLES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                  </select>
                </th>
                <th>
                  <select aria-label="按用户状态筛选" value={userColumnFilters.status} onChange={(event) => updateUserColumnFilter("status", event.currentTarget.value as UserColumnFilters["status"])}>
                    <option value="all">全部</option>
                    <option value="enabled">正常</option>
                    <option value="disabled">已禁用</option>
                  </select>
                </th>
                <th><input aria-label="按飞书绑定筛选" value={userColumnFilters.feishu} onChange={(event) => updateUserColumnFilter("feishu", event.currentTarget.value)} placeholder="open_id 或未绑定" /></th>
                <th aria-label="用户操作筛选占位" />
              </tr>
            </thead>
            <tbody>
              {visibleUsers.map((user) => {
              const isSelf = user.id === currentUserId;
              const locked = user.protected || isSelf || !canWriteUsers;
              const feishuLocked = !canWriteUsers;
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
                    {user.feishu_open_id ? (
                      <button
                        type="button"
                        disabled={feishuLocked || unbindFeishu.isPending}
                        onClick={() => {
                          setMessage(null);
                          setFeishuTarget(null);
                          setFeishuOpenId("");
                          unbindFeishu.mutate(user);
                        }}
                      >
                        解绑飞书
                      </button>
                    ) : (
                      <button
                        type="button"
                        disabled={feishuLocked}
                        onClick={() => {
                          setMessage(null);
                          setResetTarget(null);
                          setFeishuTarget(user);
                          setFeishuOpenId(user.feishu_open_id ?? "");
                        }}
                      >
                        绑定飞书
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={locked}
                      onClick={() => {
                        setMessage(null);
                        setResetTarget(null);
                        setResetPassword("");
                        setEditTarget(user);
                        setEditUsername(user.username);
                        setEditRole(user.role);
                        setEditDisabled(user.disabled);
                      }}
                    >
                      编辑
                    </button>
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
        )}
      </article>
    </section>
  );
}
