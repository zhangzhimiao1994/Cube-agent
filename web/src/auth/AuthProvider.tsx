import {
  Fragment,
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import { QueryClient, QueryClientContext, QueryClientProvider } from "@tanstack/react-query";

import { ApiError, api, type CurrentUser } from "../api/client";

type AuthState = {
  user: CurrentUser | null;
  loading: boolean;
  login(username: string, password: string): Promise<void>;
  setup(code: string, username: string, password: string): Promise<void>;
  logout(): Promise<void>;
  hasPermission(permission: string): boolean;
};

const AuthContext = createContext<AuthState | null>(null);
const cacheActors = new WeakMap<QueryClient, string>();
const actorKey = (user: CurrentUser) => JSON.stringify([user.tenant_id, user.user_id, user.role]);

export function AuthProvider({ children }: { children: ReactNode }) {
  const inheritedClient = useContext(QueryClientContext);
  const [cacheClient, setCacheClient] = useState(inheritedClient);
  const cacheClientRef = useRef(cacheClient);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const identityVersion = useRef(0);

  useEffect(() => {
    const deniedQueries = new WeakMap<object, ApiError>();
    return cacheClient?.getQueryCache().subscribe((event) => {
      if (event.type !== "updated") return;
      if (event.action.type === "error") {
        const { error } = event.query.state;
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
          deniedQueries.set(event.query, error);
          event.query.setState({ data: undefined, dataUpdatedAt: 0 });
        }
      } else if (event.action.type === "success") {
        const denial = deniedQueries.get(event.query);
        if (denial && event.action.manual) {
          // A late mutation cannot restore access. Only a successful fresh read can.
          event.query.setState({ data: undefined, dataUpdatedAt: 0, error: denial, status: "error" });
        } else if (!event.action.manual) {
          deniedQueries.delete(event.query);
        }
      }
    });
  }, [cacheClient]);

  function retireCache() {
    const previous = cacheClientRef.current;
    if (!previous) return;
    // Late queries and mutation callbacks keep the retired client, never the next identity's cache.
    void previous.cancelQueries();
    previous.clear();
    const next = new QueryClient({ defaultOptions: previous.getDefaultOptions() });
    cacheClientRef.current = next;
    setCacheClient(next);
  }

  useEffect(() => {
    let active = true;
    const version = identityVersion.current;
    api
      .me()
      .then((current) => {
        if (active && version === identityVersion.current) {
          const client = cacheClientRef.current;
          const previousActor = client && cacheActors.get(client);
          if (previousActor && previousActor !== actorKey(current)) retireCache();
          if (cacheClientRef.current) cacheActors.set(cacheClientRef.current, actorKey(current));
          setUser(current);
        }
      })
      .catch(() => {
        if (active && version === identityVersion.current) setUser(null);
      })
      .finally(() => {
        if (active && version === identityVersion.current) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function changeIdentity(action: () => Promise<CurrentUser | null>) {
    const version = ++identityVersion.current;
    setLoading(true);
    setUser(null);
    retireCache();
    try {
      const current = await action();
      if (version === identityVersion.current) {
        if (current && cacheClientRef.current) cacheActors.set(cacheClientRef.current, actorKey(current));
        setUser(current);
      }
    } finally {
      if (version === identityVersion.current) setLoading(false);
    }
  }

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      async login(username, password) {
        await changeIdentity(() => api.login(username, password));
      },
      async setup(code, username, password) {
        await changeIdentity(() => api.setup(code, username, password));
      },
      async logout() {
        await changeIdentity(async () => { await api.logout(); return null; });
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

  return <AuthContext.Provider value={value}>{cacheClient
    ? <QueryClientProvider client={cacheClient}>{children}</QueryClientProvider>
    : children}</AuthContext.Provider>;
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
  return <Fragment key={actorKey(auth.user)}>{children}</Fragment>;
}
