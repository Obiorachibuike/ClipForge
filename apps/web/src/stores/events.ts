import { create } from 'zustand';
import type { ConnectionState, ServerEvent } from '@/ws/client';

/**
 * Live-connection state and the most recent events.
 *
 * Deliberately tiny: components subscribe to derived hooks (see `useLiveJob`)
 * instead of copying server state in here. Nothing large (no video bytes, no
 * transcripts) is ever stored in a client store.
 */
interface EventsState {
  connection: ConnectionState;
  lastEventAt: number | null;
  lastError: string | null;
  /** jobId -> latest serialized job, kept fresh from websocket events. */
  jobs: Record<string, Record<string, unknown>>;
  /** Bumped whenever a channel should refetch from REST (recovery after gaps). */
  resyncToken: number;
  setConnection: (state: ConnectionState) => void;
  ingest: (event: ServerEvent) => void;
  requestResync: () => void;
  reset: () => void;
}

const JOB_EVENTS = new Set([
  'job.created',
  'job.started',
  'job.progress',
  'job.completed',
  'job.failed',
  'job.cancelled',
  'job.retrying',
]);

export const useEventsStore = create<EventsState>((set) => ({
  connection: 'idle',
  lastEventAt: null,
  lastError: null,
  jobs: {},
  resyncToken: 0,

  setConnection: (connection) =>
    set((state) => ({
      connection,
      lastError: connection === 'closed' ? state.lastError : state.lastError,
    })),

  ingest: (event) =>
    set((state) => {
      const next: Partial<EventsState> = { lastEventAt: Date.now() };
      if (JOB_EVENTS.has(event.type) && event.job_id && event.payload) {
        next.jobs = { ...state.jobs, [event.job_id]: event.payload };
      }
      if (event.type === 'system.error') {
        next.lastError = String(event.payload?.message || 'Live connection error');
      }
      return next;
    }),

  requestResync: () => set((state) => ({ resyncToken: state.resyncToken + 1 })),
  reset: () => set({ jobs: {}, lastError: null, resyncToken: 0 }),
}));
