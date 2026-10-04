import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useAuthStore } from '@/stores/auth';
import { useEventsStore } from '@/stores/events';
import { LiveClient, type ConnectionState, type ServerEvent } from '@/ws/client';

interface LiveContextValue {
  connection: ConnectionState;
  resync: () => void;
  subscribe: (channels: string[]) => void;
}

const LiveContext = createContext<LiveContextValue>({
  connection: 'idle',
  resync: () => undefined,
  subscribe: () => undefined,
});

export function LiveProvider({
  children,
  projectId,
}: {
  children: React.ReactNode;
  projectId?: string | null;
}) {
  const status = useAuthStore((state) => state.status);
  const clientRef = useRef<LiveClient | null>(null);
  const queryClient = useQueryClient();
  const connection = useEventsStore((state) => state.connection);

  const invalidateFor = useCallback(
    (event: ServerEvent) => {
      const projectId = (event.project_id as string) || null;
      const clipId = (event.payload?.clip_id as string) || null;
      const type = event.type;

      const jobs = { queryKey: queryKeys.jobsActive };
      const projects = { queryKey: queryKeys.projects() };

      if (type.startsWith('job.')) {
        queryClient.invalidateQueries(jobs);
        if (projectId) queryClient.invalidateQueries({ queryKey: queryKeys.projectJobs(projectId) });
        queryClient.invalidateQueries(projects);
        // The job payload carries the clip/video it touched; refresh that view too.
        const payload = event.payload || {};
        const projectFromPayload = (payload.project_id as string) || projectId;
        if (projectFromPayload) queryClient.invalidateQueries({ queryKey: queryKeys.project(projectFromPayload) });
        if (type === 'job.completed' || type === 'job.failed') {
          const videoId = payload.video_id as string | undefined;
          if (projectFromPayload && (payload.transcript_id || payload.words)) {
            queryClient.invalidateQueries({ queryKey: queryKeys.projectTranscript(projectFromPayload) });
          }
          if (videoId) queryClient.invalidateQueries({ queryKey: queryKeys.projectFraming(projectFromPayload!) });
          if (projectFromPayload) {
            queryClient.invalidateQueries({ queryKey: queryKeys.candidates(projectFromPayload) });
            queryClient.invalidateQueries({ queryKey: queryKeys.usage });
          }
        }
      }

      if (type === 'candidate.created' && projectId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.candidates(projectId) });
      }

      if (type.startsWith('clip.')) {
        if (projectId) {
          queryClient.invalidateQueries({ queryKey: queryKeys.clips(projectId) });
          queryClient.invalidateQueries({ queryKey: queryKeys.candidates(projectId) });
        }
        if (clipId) queryClient.invalidateQueries({ queryKey: queryKeys.clip(clipId) });
      }

      if (type.startsWith('render.')) {
        const renderId = (event.payload?.render_id as string) || null;
        if (clipId) queryClient.invalidateQueries({ queryKey: queryKeys.renders(clipId) });
        if (renderId) queryClient.invalidateQueries({ queryKey: queryKeys.render(renderId) });
        if (projectId) {
          queryClient.invalidateQueries({ queryKey: queryKeys.projectRenders(projectId) });
          queryClient.invalidateQueries({ queryKey: queryKeys.exports(projectId) });
        }
      }

      if (type === 'export.ready') {
        queryClient.invalidateQueries({ queryKey: queryKeys.exports(projectId ?? undefined) });
      }

      if (type === 'transcript.ready' && projectId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.projectTranscript(projectId) });
      }

      if (type === 'usage.updated') {
        queryClient.invalidateQueries({ queryKey: queryKeys.usage });
      }
    },
    [queryClient],
  );

  useEffect(() => {
    if (status !== 'authenticated') {
      clientRef.current?.disconnect();
      clientRef.current = null;
      useEventsStore.getState().reset();
      return;
    }

    const client = new LiveClient({
      projectId: projectId ?? null,
      getTicket: async () => {
        try {
          const response = await api.post<{ ticket: string }>('/ws-ticket');
          return response.ticket;
        } catch {
          // Cookie auth still works for the first connection attempt.
          return null;
        }
      },
      onEvent: (event) => {
        useEventsStore.getState().ingest(event);
        invalidateFor(event);
      },
      onState: (state) => useEventsStore.getState().setConnection(state),
      onResync: (payload) => {
        const jobs = (payload.jobs as ServerEvent[]) || [];
        const store = useEventsStore.getState();
        jobs.forEach((job) => store.ingest(job));
        // Recovery after a refresh or a dropped socket always re-reads REST state.
        queryClient.invalidateQueries({ queryKey: queryKeys.jobsActive });
        queryClient.invalidateQueries({ queryKey: queryKeys.projects() });
        if (projectId) {
          queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
          queryClient.invalidateQueries({ queryKey: queryKeys.projectJobs(projectId) });
        }
      },
    });

    clientRef.current = client;
    client.connect();
    return () => {
      client.disconnect();
      clientRef.current = null;
    };
  }, [status, projectId, invalidateFor, queryClient]);

  const value = useMemo<LiveContextValue>(
    () => ({
      connection,
      resync: () => clientRef.current?.resync(),
      subscribe: (channels: string[]) => clientRef.current?.subscribe(channels),
    }),
    [connection],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext);
}
