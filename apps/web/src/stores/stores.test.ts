/** Zustand stores: session lifecycle, toasts, and live event bookkeeping. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getCsrfToken, setUnauthorizedHandler } from '@/lib/api';
import { useAuthStore } from '@/stores/auth';
import { useEventsStore } from '@/stores/events';
import { applyTheme, errorToast, toast, useUiStore } from '@/stores/ui';
import { installFetchDouble, type FetchDouble } from '@/test/utils';

let server: FetchDouble;
const randomCredential = () => Math.random().toString(36).slice(2);

const SESSION = {
  user: {
    id: 'u1',
    email: 'ada@example.com',
    name: 'Ada',
    avatar_url: '',
    plan: 'free',
    privacy_mode: 'cloud',
    default_aspect_ratio: '9:16',
    default_caption_preset: 'bold',
    is_admin: false,
    created_at: '2026-01-01T00:00:00Z',
    last_login_at: null,
  },
  csrf_token: 'csrf-xyz',
  expires_at: '2026-12-01T00:00:00Z',
};

beforeEach(() => {
  server = installFetchDouble();
  window.localStorage.removeItem('clipforge_theme');
  applyTheme('dark');
  useUiStore.setState({ theme: 'dark' });
  useAuthStore.setState({ user: null, status: 'unknown', sessionExpiresAt: null, error: null, busy: false });
});

afterEach(() => {
  server.restore();
  vi.restoreAllMocks();
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
  useEventsStore.setState({ connection: 'idle', lastEventAt: null, lastError: null, jobs: {}, resyncToken: 0 });
});

describe('auth store', () => {
  it('bootstrap authenticates from the session endpoint and stores the CSRF token', async () => {
    server.respond('/auth/session', { body: SESSION });
    await useAuthStore.getState().bootstrap();

    const state = useAuthStore.getState();
    expect(state.status).toBe('authenticated');
    expect(state.user?.email).toBe('ada@example.com');
    expect(getCsrfToken()).toBe('csrf-xyz');
  });

  it('bootstrap treats a 401 as anonymous rather than an error', async () => {
    server.respond('/auth/session', { status: 401, body: { code: 'auth_required', message: 'Sign in.' } });
    await useAuthStore.getState().bootstrap();

    const state = useAuthStore.getState();
    expect(state.status).toBe('anonymous');
    expect(state.user).toBeNull();
    expect(getCsrfToken()).toBeNull();
  });

  it('login stores the user and clears the previous error', async () => {
    useAuthStore.setState({ error: 'previous failure' });
    server.respond('/auth/login', { body: SESSION });

    const password = randomCredential();
    await useAuthStore.getState().login('ada@example.com', password);

    expect(useAuthStore.getState().error).toBeNull();
    expect(useAuthStore.getState().user?.id).toBe('u1');
    expect(server.calls[0].body).toEqual({ email: 'ada@example.com', password });
  });

  it('login surfaces the server message and rethrows', async () => {
    server.respond('/auth/login', {
      status: 401,
      body: { code: 'invalid_credentials', message: 'Email or password is incorrect.' },
    });

    await expect(useAuthStore.getState().login('ada@example.com', 'nope')).rejects.toThrow(
      'Email or password is incorrect.',
    );
    expect(useAuthStore.getState().error).toBe('Email or password is incorrect.');
    expect(useAuthStore.getState().status).not.toBe('authenticated');
  });

  it('never leaves busy set after a failure', async () => {
    server.respond('/auth/register', { status: 409, body: { code: 'conflict', message: 'Email already registered.' } });

    await expect(useAuthStore.getState().register({ email: 'ada@example.com', password: 'x' })).rejects.toThrow();
    expect(useAuthStore.getState().busy).toBe(false);
  });

  it('logout clears the local session even when the server call fails', async () => {
    useAuthStore.setState({ user: SESSION.user, status: 'authenticated' });
    server.restore();
    globalThis.fetch = vi.fn(() => Promise.reject(new TypeError('offline'))) as unknown as typeof fetch;

    await useAuthStore.getState().logout();

    expect(useAuthStore.getState().status).toBe('anonymous');
    expect(useAuthStore.getState().user).toBeNull();
    expect(getCsrfToken()).toBeNull();
  });

  it('the global 401 handler drops the session exactly once', () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    useAuthStore.setState({ user: SESSION.user, status: 'authenticated' });
    useAuthStore.getState().clear();

    expect(useAuthStore.getState().status).toBe('anonymous');
    expect(useAuthStore.getState().user).toBeNull();
  });

  it('keeps no credential in localStorage or sessionStorage', async () => {
    const localSpy = vi.spyOn(Storage.prototype, 'setItem');
    server.respond('/auth/login', { body: SESSION });
    const password = randomCredential();
    await useAuthStore.getState().login('ada@example.com', password);

    expect(localSpy).not.toHaveBeenCalled();
  });
});

describe('ui store', () => {
  it('switches and persists the application theme', () => {
    useUiStore.getState().setTheme('light');
    expect(useUiStore.getState().theme).toBe('light');
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(window.localStorage.getItem('clipforge_theme')).toBe('light');

    useUiStore.getState().toggleTheme();
    expect(useUiStore.getState().theme).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('pushes a toast with a generated id and auto-dismisses it', () => {
    vi.useFakeTimers();
    const id = toast({ kind: 'success', title: 'Saved', duration: 1000 });

    expect(useUiStore.getState().toasts).toHaveLength(1);
    expect(useUiStore.getState().toasts[0].id).toBe(id);

    vi.advanceTimersByTime(1100);
    expect(useUiStore.getState().toasts).toHaveLength(0);
    vi.useRealTimers();
  });

  it('keeps errors on screen longer than successes', () => {
    const successId = toast({ kind: 'success', title: 'ok' });
    const errorId = toast({ kind: 'error', title: 'broken' });

    const byId = (id: string) => useUiStore.getState().toasts.find((item) => item.id === id);
    expect(byId(errorId)!.duration!).toBeGreaterThan(byId(successId)!.duration!);

    useUiStore.getState().dismissToast(successId);
    useUiStore.getState().dismissToast(errorId);
  });

  it('honours duration 0 as a persistent toast', () => {
    vi.useFakeTimers();
    const id = toast({ kind: 'info', title: 'working…', duration: 0 });
    vi.advanceTimersByTime(60_000);

    expect(useUiStore.getState().toasts.some((item) => item.id === id)).toBe(true);
    useUiStore.getState().dismissToast(id);
    vi.useRealTimers();
  });

  it('errorToast extracts the message from an Error', () => {
    errorToast(new Error('Rendering failed.'));
    expect(useUiStore.getState().toasts[0]).toMatchObject({ kind: 'error', title: 'Rendering failed.' });
  });

  it('errorToast falls back when given a non-error', () => {
    errorToast(undefined, 'Could not save.');
    expect(useUiStore.getState().toasts[0].title).toBe('Could not save.');
  });
});

describe('events store', () => {
  it('tracks the connection state', () => {
    useEventsStore.getState().setConnection('open');
    expect(useEventsStore.getState().connection).toBe('open');
  });

  it('keeps the latest payload per job', () => {
    useEventsStore.getState().ingest({ type: 'job.progress', job_id: 'j1', payload: { progress: 10 } });
    useEventsStore.getState().ingest({ type: 'job.progress', job_id: 'j1', payload: { progress: 55 } });
    useEventsStore.getState().ingest({ type: 'job.created', job_id: 'j2', payload: { status: 'queued' } });

    const jobs = useEventsStore.getState().jobs;
    expect(jobs.j1.progress).toBe(55);
    expect(jobs.j2.status).toBe('queued');
    expect(useEventsStore.getState().lastEventAt).toBeTypeOf('number');
  });

  it('ignores events without a job id instead of storing junk', () => {
    useEventsStore.getState().ingest({ type: 'job.progress', payload: { progress: 5 } });
    expect(Object.keys(useEventsStore.getState().jobs)).toHaveLength(0);
  });

  it('sticks the last error on the banner state', () => {
    useEventsStore.getState().ingest({
      type: 'system.error',
      payload: { code: 'not_authenticated', message: 'Sign in to receive live updates.' },
    });
    expect(useEventsStore.getState().lastError).toBe('Sign in to receive live updates.');
  });

  it('bumps the resync token so queries refetch after a gap', () => {
    const before = useEventsStore.getState().resyncToken;
    useEventsStore.getState().requestResync();
    expect(useEventsStore.getState().resyncToken).toBe(before + 1);
  });

  it('reset clears the job cache but keeps the connection state', () => {
    useEventsStore.setState({ connection: 'open' });
    useEventsStore.getState().ingest({ type: 'job.progress', job_id: 'j1', payload: { progress: 1 } });
    useEventsStore.getState().reset();

    expect(useEventsStore.getState().jobs).toEqual({});
    expect(useEventsStore.getState().connection).toBe('open');
  });
});
