import { QueryClient } from '@tanstack/react-query';
import { ApiError } from '@/lib/api';

/**
 * Query defaults tuned for a media app.
 *
 * Polling is deliberately conservative: when the live socket is connected the
 * UI is driven by events, and `refetchInterval` per hook only kicks in while
 * the socket is down (see `useJobs`).
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
      retry: (failureCount, error) => {
        if (error instanceof ApiError) {
          if (error.status === 401 || error.status === 403 || error.status === 404) return false;
          if (error.status >= 500) return failureCount < 2;
          if (error.isRateLimited) return false;
        }
        return failureCount < 2;
      },
      retryDelay: (attempt) => Math.min(8000, 600 * 2 ** attempt),
    },
    mutations: {
      retry: 0,
    },
  },
});

export const queryKeys = {
  session: ['session'] as const,
  capabilities: ['capabilities'] as const,
  settings: ['settings'] as const,
  usage: ['usage'] as const,
  usageHistory: (range: string) => ['usage', 'history', range] as const,
  billing: ['billing'] as const,
  plans: ['billing', 'plans'] as const,
  projects: (params?: Record<string, unknown>) => ['projects', params ?? {}] as const,
  project: (id: string) => ['project', id] as const,
  projectJobs: (id: string) => ['project', id, 'jobs'] as const,
  projectTranscript: (id: string) => ['project', id, 'transcript'] as const,
  projectFraming: (id: string) => ['project', id, 'framing'] as const,
  transcript: (id: string) => ['transcript', id] as const,
  videoScript: (videoId: string) => ['video', videoId, 'narration-script'] as const,
  transcriptWindow: (id: string, start: number, end: number) => ['transcript', id, 'window', start, end] as const,
  candidates: (projectId: string, params?: Record<string, unknown>) =>
    ['project', projectId, 'candidates', params ?? {}] as const,
  candidateScore: (id: string) => ['candidate', id, 'score'] as const,
  clips: (projectId: string, params?: Record<string, unknown>) =>
    ['project', projectId, 'clips', params ?? {}] as const,
  clip: (id: string) => ['clip', id] as const,
  captions: (clipId: string) => ['clip', clipId, 'captions'] as const,
  headlines: (clipId: string) => ['clip', clipId, 'headlines'] as const,
  renders: (clipId: string) => ['clip', clipId, 'renders'] as const,
  render: (id: string) => ['render', id] as const,
  projectRenders: (projectId: string) => ['project', projectId, 'renders'] as const,
  exports: (projectId?: string) =>
    (projectId ? ['project', projectId, 'exports'] : ['exports']) as readonly unknown[],
  jobsActive: ['jobs', 'active'] as const,
  jobs: (params?: Record<string, unknown>) => ['jobs', params ?? {}] as const,
  captionStyles: ['caption-styles'] as const,
  captionPresets: ['caption-presets'] as const,
  renderPresets: ['render-presets'] as const,
  providers: ['ai-providers'] as const,
};
