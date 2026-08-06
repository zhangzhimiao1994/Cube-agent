import { z } from "zod";

const MeSchema = z.object({
  username: z.string(),
  role: z.string(),
  permissions: z.array(z.string()),
});

export type CurrentUser = z.infer<typeof MeSchema>;

const UserSchema = z.object({
  id: z.string(),
  username: z.string(),
  role: z.string(),
  disabled: z.boolean(),
  feishu_open_id: z.string().nullable(),
});

export type ManagedUser = z.infer<typeof UserSchema>;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init: RequestInit,
  schema: z.ZodType<T>,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    throw new ApiError("request failed", response.status);
  }
  const payload = await response.json();
  return schema.parse(payload);
}

export const api = {
  me(): Promise<CurrentUser> {
    return request("/api/v1/me", { method: "GET" }, MeSchema);
  },
  login(username: string, password: string): Promise<CurrentUser> {
    return request(
      "/api/v1/auth/login",
      { method: "POST", body: JSON.stringify({ username, password }) },
      MeSchema,
    );
  },
  setup(code: string, username: string, password: string): Promise<CurrentUser> {
    return request(
      "/api/v1/setup",
      { method: "POST", body: JSON.stringify({ code, username, password }) },
      MeSchema,
    );
  },
  async logout(): Promise<void> {
    await fetch("/api/v1/auth/logout", {
      method: "POST",
      credentials: "include",
    });
  },
  users(): Promise<ManagedUser[]> {
    return request("/api/v1/users", { method: "GET" }, z.array(UserSchema));
  },
  changeUserRole(userId: string, role: string): Promise<ManagedUser> {
    return request(
      `/api/v1/users/${userId}/role`,
      { method: "PATCH", body: JSON.stringify({ role }) },
      UserSchema,
    );
  },
};
