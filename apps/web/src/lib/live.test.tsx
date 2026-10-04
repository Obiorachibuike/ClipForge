/**
 * LiveProvider: socket events must invalidate exactly the queries they affect,
 * and a resync must always re-read REST state (recovery after a refresh).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LiveProvider, useLive } from '@/lib/live';
import { queryKeys } from '@/lib/query';
import { useAuthStore } from '@/stores/auth';
import { useEventsStore } from '@/stores/events';
import { FakeWebSocket, createTestQueryClient, installFakeWebSocket, installFetchDouble, type FetchDouble } from '@/test/utils';

let server: FetchDouble;
let restoreSocket: () => void;

function Probe() {
  const { connection } = useLive();
  return <span data-testid="connection">{connection}</span>;
}

function wrapperFor(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

function mountProvider(queryClient: QueryClient, projectId?: string) {
  return render(
    <LiveProvider projectId={projectId}>
      <Probe />
    </LiveProvider>,
    { wrapper: wrapperFor(queryClient) },
  );
}

/** Spy on invalidation with the same key shapes the app uses. */
function spyOnInvalidation(queryClient: QueryClient) {
  const calls: unknown[][] = [];
  const original = queryClient.invalidateQueries.bind(queryClient);
  vi.spyOn(queryClient, 'invalidateQueries').mockImplementation((filters?: Parameters<typeof original>[0]) => {
    calls.push([filters?.queryKey]);
    return original(filters);
  });
  return { calls, invalidated: (key: unknown) => calls.some(([candidate]) => sameKey(candidate, key)) };
}

function sameKey(a: unknown, b: unknown): boolean {
  if (!Array.isArray(a) || !Array.isArray(b)) return JSON.stringify(a) === JSON.stringify(b);
  if (a.length < b.length) return false;
  return JSON.stringify(a.slice(0, b.length)) === JSON.stringify(b);
}

beforeEach(() => {
  server = installFetchDouble();
  restoreSocket = installFakeWebSocket();
  server.respond('/ws-ticket', { body: { ticket: 't-1', expires_in: 60 } });
  useAuthStore.setState({ status: 'authenticated', user: { id: 'u1' } as never, error: null, busy: false });
  useEventsStore.setState({ connection: 'idle', lastEventAt: null, lastError: null, jobs: {}, resyncToken: 0 });
});

afterEach(() => {
  server.restore();
  restoreSocket();
  vi.restoreAllMocks();
  useAuthStore.setState({ status: 'anonymous', user: null });
});

describe('LiveProvider', () => {
  it('does not open a socket while signed out', async () => {
    useAuthStore.setState({ status: 'anonymous' });
    const queryClient = createTestQueryClient();
    render(
      <LiveProvider>
        <Probe />
      </LiveProvider>,
      { wrapper: wrapperFor(queryClient) },
    );

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(0));
  });

  it('connects and reports the state to the UI', async () => {
    const queryClient = createTestQueryClient();
    mountProvider(queryClient);

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    await waitFor(() => expect(screen.getByTestId('connection')).toHaveTextContent('open'));
  });

  it('invalidates the active-jobs and project queries when a job progresses', async () => {
    const queryClient = createTestQueryClient();
    const spy = spyOnInvalidation(queryClient);
    mountProvider(queryClient, 'proj-1');
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    FakeWebSocket.last.receive({
      type: 'job.progress',
      job_id: 'job-1',
      project_id: 'proj-1',
      payload: { progress: 40, stage: 'transcribing', project_id: 'proj-1' },
    });

    await waitFor(() => {
      expect(spy.invalidated(queryKeys.jobsActive)).toBe(true);
      expect(spy.invalidated(queryKeys.projectJobs('proj-1'))).toBe(true);
      expect(spy.invalidated(queryKeys.project('proj-1'))).toBe(true);
    });
  });

  it('refreshes candidates, transcript and usage only once a job completes', async () => {
    const queryClient = createTestQueryClient();
    const spy = spyOnInvalidation(queryClient);
    mountProvider(queryClient, 'proj-1');
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    FakeWebSocket.last.receive({
      type: 'job.progress',
      job_id: 'job-1',
      project_id: 'proj-1',
      payload: { progress: 50, project_id: 'proj-1' },
    });
    await waitFor(() => expect(spy.invalidated(queryKeys.candidates('proj-1'))).toBe(false));

    FakeWebSocket.last.receive({
      type: 'job.completed',
      job_id: 'job-1',
      project_id: 'proj-1',
      payload: { project_id: 'proj-1', transcript_id: 'tr-1', words: 409 },
    });

    await waitFor(() => {
      expect(spy.invalidated(queryKeys.candidates('proj-1'))).toBe(true);
      expect(spy.invalidated(queryKeys.projectTranscript('proj-1'))).toBe(true);
      expect(spy.invalidated(queryKeys.usage)).toBe(true);
    });
  });

  it('refreshes clip, caption and render queries on clip and render events', async () => {
    const queryClient = createTestQueryClient();
    const spy = spyOnInvalidation(queryClient);
    mountProvider(queryClient, 'proj-1');
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    FakeWebSocket.last.receive({
      type: 'render.progress',
      job_id: 'job-9',
      project_id: 'proj-1',
      payload: { clip_id: 'clip-7', render_id: 'render-3', progress: 20 },
    });

    await waitFor(() => {
      expect(spy.invalidated(queryKeys.clip('clip-7'))).toBe(true);
      expect(spy.invalidated(queryKeys.renders('clip-7'))).toBe(true);
      expect(spy.invalidated(queryKeys.render('render-3'))).toBe(true);
      expect(spy.invalidated(queryKeys.projectRenders('proj-1'))).toBe(true);
      expect(spy.invalidated(queryKeys.exports('proj-1'))).toBe(true);
    });
  });

  it('re-reads REST state on a resync', async () => {
    const queryClient = createTestQueryClient();
    const spy = spyOnInvalidation(queryClient);
    mountProvider(queryClient, 'proj-1');
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    FakeWebSocket.last.receive({
      type: 'system.resync',
      payload: { jobs: [{ type: 'job.progress', job_id: 'job-2', payload: { progress: 80 } }] },
    });

    await waitFor(() => {
      expect(spy.invalidated(queryKeys.jobsActive)).toBe(true);
      expect(spy.invalidated(queryKeys.projects())).toBe(true);
      expect(spy.invalidated(queryKeys.project('proj-1'))).toBe(true);
      expect(spy.invalidated(queryKeys.projectJobs('proj-1'))).toBe(true);
    });
  });

  it('stores resynced job snapshots so late subscribers see them', async () => {
    const queryClient = createTestQueryClient();
    mountProvider(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    FakeWebSocket.last.receive({
      type: 'system.resync',
      payload: { jobs: [{ type: 'job.progress', job_id: 'job-2', payload: { progress: 80 } }] },
    });

    await waitFor(() => expect(useEventsStore.getState().jobs['job-2']).toMatchObject({ progress: 80 }));
  });

  it('disconnects and clears live state on sign-out', async () => {
    const queryClient = createTestQueryClient();
    mountProvider(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    FakeWebSocket.last.open();

    useAuthStore.setState({ status: 'anonymous' });

    await waitFor(() => expect(useEventsStore.getState().jobs).toEqual({}));
    expect(FakeWebSocket.last.closeCalls.length).toBeGreaterThan(0);
  });

  it('requests a ticket through the API and never leaks the session token into the URL', async () => {
    const queryClient = createTestQueryClient();
    mountProvider(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    expect(server.calls.some((call) => call.url.includes('/ws-ticket'))).toBe(true);
    expect(FakeWebSocket.last.url).toContain('ticket=t-1');
    expect(FakeWebSocket.last.url).not.toMatch(/csrf|session/i);
  });
});
