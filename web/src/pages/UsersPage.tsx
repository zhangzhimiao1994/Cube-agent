import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatApiError } from "../api/client";

export function UsersPage() {
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.users() });

  async function changeRole(userId: string, role: string) {
    await api.changeUserRole(userId, role);
    await queryClient.invalidateQueries({ queryKey: ["users"] });
  }

  if (users.isLoading) return <p>加载用户...</p>;
  if (users.isError) {
    return <p role="alert">{formatApiError(users.error, "用户列表加载失败")}</p>;
  }
  return (
    <section>
      <h2>用户管理</h2>
      <table>
        <thead>
          <tr>
            <th>用户名</th>
            <th>角色</th>
            <th>飞书</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {users.data?.map((user) => (
            <tr key={user.id}>
              <td>{user.username}</td>
              <td>{user.role}</td>
              <td>{user.feishu_open_id ?? "未绑定"}</td>
              <td>
                <button type="button" onClick={() => void changeRole(user.id, "admin")}>
                  设为管理员
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
