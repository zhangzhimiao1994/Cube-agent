import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Navigate, useLocation } from "react-router-dom";

import { api, type CurrentUser } from "../api/client";

type AuthState = {
  user: CurrentUser | null;
  loading: boolean;
  login(username: string, password: string): Promise<void>;
  setup(code: string, username: string, password: string): Promise<void>;
  logout(): Promise<void>;
  hasPermission(permission: string): boolean;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    api
      .me()
      .then((current) => {
        if (active) setUser(current);
      })
      .catch(() => {
        if (active) setUser(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      async login(username, password) {
        setUser(await api.login(username, password));
      },
      async setup(code, username, password) {
        setUser(await api.setup(code, username, password));
      },
      async logout() {
        await api.logout();
        setUser(null);
      },
      hasPermission(permission) {
        if (user === null) return false;
        return user.permissions.some(
          (granted) =>
            granted === "*" ||
            granted === permission ||
            (granted.endsWith(":*") && permission.startsWith(granted.slice(0, -1))),
        );
      },
    }),
    [loading, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();
  if (auth.loading) return <p>Loading...</p>;
  if (auth.user === null) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}
