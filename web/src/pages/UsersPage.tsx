import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError } from "../api/client";

const ROLES = [
  { value: "super_admin", label: "超级管理员", description: "拥有所有权限，至少保留一个。" },
  { value: "admin", label: "管理员", description: "可管理配置、模型、Agent、Skill 和工具。" },
  { value: "operator", label: "操作员", description: "可创建和控制任务，可读取配置。" },
  { value: "viewer", label: "只读用户", description: "只能查看任务、配置和审计。" },
] as const;

function roleLabel(role: string) {
  return ROLES.find((item) => item.value === role)?.label ?? role;
}

export function UsersPage() {
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.users() });
  const changeRole = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) => api.changeUserRole(userId, role),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  if (users.isLoading) return <p>加载用户...</p>;
  if (users.isError) {
    return <p role="alert">{formatApiError(users.error, "用户列表加载失败")}</p>;
  }

  return (
    <section>
      <p className="eyebrow">Access control</p>
      <h2>用户管理</h2>
      <p>这里管理控制台账号角色。系统会阻止移除最后一个超级管理员。</p>

      <article>
        <h3>角色说明</h3>
        <div className="card-grid compact">
          {ROLES.map((role) => (
            <div key={role.value} className="mini-card">
              <strong>{role.label}</strong>
              <p>{role.description}</p>
            </div>
          ))}
        </div>
      </article>

      {changeRole.isError ? <p role="alert">{formatApiError(changeRole.error, "角色修改失败")}</p> : null}

      <table>
        <thead>
          <tr>
            <th>用户名</th>
            <th>当前角色</th>
            <th>飞书绑定</th>
            <th>修改角色</th>
          </tr>
        </thead>
        <tbody>
          {users.data?.map((user) => (
            <tr key={user.id}>
              <td>{user.username}</td>
              <td>{roleLabel(user.role)}</td>
              <td>{user.feishu_open_id ?? "未绑定"}</td>
              <td>
                <select
                  aria-label={`修改 ${user.username} 的角色`}
                  value={user.role}
                  onChange={(event) =>
                    changeRole.mutate({ userId: user.id, role: event.currentTarget.value })
                  }
                >
                  {ROLES.map((role) => (
                    <option key={role.value} value={role.value}>
                      {role.label}
                    </option>
                  ))}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
