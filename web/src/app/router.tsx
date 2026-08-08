import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, MemoryRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./AppShell";
import { AuthProvider, RequireAuth } from "../auth/AuthProvider";
import { AgentsPage } from "../pages/AgentsPage";
import { AuditPage } from "../pages/AuditPage";
import { ConfigPage } from "../pages/ConfigPage";
import { HermesPage } from "../pages/HermesPage";
import { LoginPage } from "../pages/LoginPage";
import { McpPage } from "../pages/McpPage";
import { MemoryPage } from "../pages/MemoryPage";
import { ModelsPage } from "../pages/ModelsPage";
import { RunDetailPage } from "../pages/RunDetailPage";
import { RunsPage } from "../pages/RunsPage";
import { SetupPage } from "../pages/SetupPage";
import { SkillsPage } from "../pages/SkillsPage";
import { UsersPage } from "../pages/UsersPage";
import { WorkflowsPage } from "../pages/WorkflowsPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
    mutations: { retry: false },
  },
});

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<RunsPage />} />
        <Route path="runs/:runId" element={<RunDetailPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="models" element={<ModelsPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="skills" element={<SkillsPage />} />
        <Route path="mcp" element={<McpPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="hermes" element={<HermesPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="audit" element={<AuditPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export function AppRouter() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export function TestApp({ initialPath = "/" }: { initialPath?: string }) {
  const testClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={testClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}
