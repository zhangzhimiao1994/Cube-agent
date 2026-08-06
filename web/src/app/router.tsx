import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, MemoryRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./AppShell";
import { AuthProvider, RequireAuth } from "../auth/AuthProvider";
import { AgentsPage } from "../pages/AgentsPage";
import { ConfigPage } from "../pages/ConfigPage";
import { LoginPage } from "../pages/LoginPage";
import { ModelsPage } from "../pages/ModelsPage";
import { SetupPage } from "../pages/SetupPage";
import { UsersPage } from "../pages/UsersPage";
import { WorkflowsPage } from "../pages/WorkflowsPage";

const queryClient = new QueryClient();

function DashboardPage() {
  return <h2>运行概览</h2>;
}

function PlaceholderPage({ title }: { title: string }) {
  return <h2>{title}</h2>;
}

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
        <Route index element={<DashboardPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="models" element={<ModelsPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="skills" element={<PlaceholderPage title="Skills" />} />
        <Route path="mcp" element={<PlaceholderPage title="MCP" />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="audit" element={<PlaceholderPage title="审计" />} />
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
  const testClient = new QueryClient();
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
