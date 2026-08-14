import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, MemoryRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./AppShell";
import { AuthProvider, RequireAuth } from "../auth/AuthProvider";
import { AgentsPage } from "../pages/AgentsPage";
import { AttachmentsPage } from "../pages/AttachmentsPage";
import { ChannelsPage } from "../pages/ChannelsPage";
import { ConfigPage } from "../pages/ConfigPage";
import { EvolutionPage } from "../pages/EvolutionPage";
import { HermesPage } from "../pages/HermesPage";
import { LoginPage } from "../pages/LoginPage";
import { LogsPage } from "../pages/LogsPage";
import { MainAgentPage } from "../pages/MainAgentPage";
import { McpPage } from "../pages/McpPage";
import { MemoryPage } from "../pages/MemoryPage";
import { ModelsPage } from "../pages/ModelsPage";
import { ModuleHubPage } from "../pages/ModuleHubPage";
import { RunDetailPage } from "../pages/RunDetailPage";
import { RunsPage } from "../pages/RunsPage";
import { SchedulesPage } from "../pages/SchedulesPage";
import { SetupPage } from "../pages/SetupPage";
import { SkillsPage } from "../pages/SkillsPage";
import { UsersPage } from "../pages/UsersPage";
import { WorkflowsPage } from "../pages/WorkflowsPage";
import { MODULE_GROUPS } from "./navigation";

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
        {MODULE_GROUPS.map((group) => (
          <Route key={group.id} path={group.to.slice(1)} element={<ModuleHubPage group={group} />} />
        ))}
        <Route path="runs/:runId" element={<RunDetailPage />} />
        <Route path="evolution" element={<EvolutionPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="main-agent" element={<MainAgentPage />} />
        <Route path="models" element={<ModelsPage />} />
        <Route path="multimedia" element={<Navigate to="/models" replace />} />
        <Route path="attachments" element={<AttachmentsPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="schedules" element={<SchedulesPage />} />
        <Route path="skills" element={<SkillsPage />} />
        <Route path="mcp" element={<McpPage />} />
        <Route path="channels" element={<ChannelsPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="hermes" element={<HermesPage />} />
        <Route path="hermes/:insightId" element={<HermesPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="logs/:module" element={<LogsPage />} />
        <Route path="audit" element={<Navigate to="/logs/audit" replace />} />
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
