import { useEffect } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { queryClient } from '@/lib/query';
import { LiveProvider } from '@/lib/live';
import { useAuthStore } from '@/stores/auth';
import AppShell from '@/components/layout/AppShell';
import ToastHost from '@/components/ToastHost';
import RequireAuth from '@/components/RequireAuth';
import Landing from '@/pages/Landing';
import AuthPage from '@/pages/Auth';
import Dashboard from '@/pages/Dashboard';
import ProjectPage from '@/pages/Project';
import AnalyzePage from '@/pages/Analyze';
import ClipsPage from '@/pages/Clips';
import EditorPage from '@/pages/Editor';
import ExportPage from '@/pages/Export';
import SettingsPage from '@/pages/Settings';
import PricingPage from '@/pages/Pricing';
import NotFound from '@/pages/NotFound';

/** Session probe gate: screens must not flash a login form before we know. */
function SessionGate({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((state) => state.status);
  const bootstrap = useAuthStore((state) => state.bootstrap);

  useEffect(() => {
    if (status === 'unknown') void bootstrap();
  }, [status, bootstrap]);

  if (status === 'unknown') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-ink-950">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-ink-600 border-t-accent-500" />
          <p className="text-sm text-slate-500">Starting ClipForge…</p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

function ProjectLiveWrapper({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const match = /^\/projects\/([^/]+)/.exec(location.pathname);
  const projectId = match?.[1] ?? null;
  return <LiveProvider projectId={projectId}>{children}</LiveProvider>;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <SessionGate>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/login" element={<AuthPage mode="login" />} />
            <Route path="/register" element={<AuthPage mode="register" />} />
            <Route path="/pricing" element={<PricingPage />} />

            <Route
              element={
                <RequireAuth>
                  <ProjectLiveWrapper>
                    <AppShell />
                  </ProjectLiveWrapper>
                </RequireAuth>
              }
            >
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/projects/:projectId" element={<ProjectPage />} />
              <Route path="/projects/:projectId/analyze" element={<AnalyzePage />} />
              <Route path="/projects/:projectId/clips" element={<ClipsPage />} />
              <Route path="/projects/:projectId/editor/:clipId" element={<EditorPage />} />
              <Route path="/projects/:projectId/export" element={<ExportPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Route>

            <Route path="/404" element={<NotFound />} />
            <Route path="*" element={<Navigate to="/404" replace />} />
          </Routes>
          <ToastHost />
        </SessionGate>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
