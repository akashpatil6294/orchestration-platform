/**
 * Route table.
 *
 * `/login` is the only public page. Everything else sits behind `RequireAuth`,
 * which checks the Supabase session before rendering the shell. The API has its
 * own, independent check: hiding a page here is a convenience, never a
 * security boundary.
 */
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AuthProvider } from './auth/AuthProvider'
import { RequireAuth } from './auth/RequireAuth'
import { AppShell } from './components/AppShell'
import { ErrorBoundary } from './components/ErrorBoundary'
import { DashboardPage } from './pages/DashboardPage'
import { DlqPage } from './pages/DlqPage'
import { LoginPage } from './pages/LoginPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { RunsPage } from './pages/RunsPage'
import { SchedulesPage } from './pages/SchedulesPage'
import { SettingsPage } from './pages/SettingsPage'
import AuditPage from './pages/AuditPage'
import ConnectionsPage from './pages/ConnectionsPage'
import DocumentsPage from './pages/DocumentsPage'
import RunComparePage from './pages/RunComparePage'
import SchedulesCalendarPage from './pages/SchedulesCalendarPage'
import TemplatesPage from './pages/TemplatesPage'
import NotificationsPage from './pages/NotificationsPage'
import TeamsPage from './pages/TeamsPage'
import { TriggersPage } from './pages/TriggersPage'
import { WorkflowDetailPage } from './pages/WorkflowDetailPage'
import WorkflowEditPage from './pages/WorkflowEditPage'
import AlertsPage from './pages/AlertsPage'
import RecoveryPage from './pages/RecoveryPage'
import { WorkflowsPage } from './pages/WorkflowsPage'
import { WorkersPage } from './pages/WorkersPage'

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/:workflowId" element={<WorkflowDetailPage />} />
          <Route path="/workflows/:workflowId/edit" element={<WorkflowEditPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/triggers" element={<TriggersPage />} />
          <Route path="/ops/workers" element={<WorkersPage />} />
          <Route path="/ops/dlq" element={<DlqPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/runs/compare" element={<RunComparePage />} />
          <Route path="/schedules/calendar" element={<SchedulesCalendarPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
          <Route path="/notifications" element={<NotificationsPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/ops/recovery" element={<RecoveryPage />} />
          <Route path="/teams" element={<TeamsPage />} />
        </Route>
      </Route>

      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}

export function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  )
}

export default App
