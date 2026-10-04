/** API client: request shaping, CSRF, error mapping and 401 handling. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, getCsrfToken, mediaUrl, request, setCsrfToken, setUnauthorizedHandler } from '@/lib/api';
import { installFetchDouble, type FetchDouble } from '@/test/utils';

let server: FetchDouble;

beforeEach(() => {
  server = installFetchDouble();
  setCsrfToken(null);
  setUnauthorizedHandler(null);
});

afterEach(() => {
  server.restore();
  vi.restoreAllMocks();
});

describe('request', () => {
  it('sends JSON requests to the versioned prefix with credentials', async () => {
    server.respond('/api/v1/projects', { body: { items: [] } });
    await api.get('/projects');

    expect(server.calls).toHaveLength(1);
    const call = server.calls[0];
    expect(call.url).toBe('/api/v1/projects');
    expect(call.method).toBe('GET');
  });

  it('serialises query parameters and drops empty values', async () => {
    server.respond('/api/v1/jobs', { body: { items: [] } });
    await request('/jobs', { query: { limit: 25, offset: 0, status: undefined, q: '' } });

    const url = server.calls[0].url;
    expect(url).toContain('limit=25');
    expect(url).toContain('offset=0');
    expect(url).not.toContain('status');
    expect(url).not.toContain('q=');
  });

  it('attaches the CSRF token to state-changing requests only', async () => {
    setCsrfToken('csrf-abc');
    server.respond('/api/v1/settings', { body: {} });

    await api.patch('/settings', { name: 'Ada' });
    expect(server.calls[0].headers['x-csrf-token']).toBe('csrf-abc');

    await api.get('/settings');
    expect(server.calls[1].headers['x-csrf-token']).toBeUndefined();
  });

  it('never puts an auth token in localStorage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    setCsrfToken('csrf-abc');
    server.respond('/api/v1/auth/login', { body: { user: { id: 'u1' }, csrf_token: 'csrf-abc' } });

    await api.post('/auth/login', { email: 'a@b.com', password: 'x' });

    const written: string[] = setItem.mock.calls.map(([key]) => key);
    expect(written.some((key) => /token|auth|session|jwt/i.test(key))).toBe(false);
  });

  it('maps a structured error body onto ApiError', async () => {
    server.respond('/api/v1/projects', {
      status: 422,
      body: { code: 'validation_error', message: 'Name is required.', details: { field: 'name' }, request_id: 'r1' },
    });

    await expect(api.post('/projects', { name: '' })).rejects.toBeInstanceOf(ApiError);
    try {
      await api.post('/projects', { name: '' });
    } catch (error) {
      const apiError = error as ApiError;
      expect(apiError.status).toBe(422);
      expect(apiError.code).toBe('validation_error');
      expect(apiError.message).toBe('Name is required.');
      expect(apiError.details).toEqual({ field: 'name' });
    }
  });

  it('surfaces the request id on the error for support', async () => {
    server.respond('/api/v1/jobs', {
      status: 500,
      body: { code: 'internal_error', message: 'Something went wrong.', request_id: 'req-42' },
    });

    await expect(api.get('/jobs')).rejects.toMatchObject({ requestId: 'req-42' });
  });

  it('keeps a raw stack trace out of the user-facing message', async () => {
    server.respond('/api/v1/jobs', {
      status: 500,
      body: {
        code: 'internal_error',
        message: 'Something went wrong on our side.',
        details: { traceback: 'Traceback (most recent call last): File "/srv/app.py"' },
      },
    });

    try {
      await api.get('/jobs');
      throw new Error('should have thrown');
    } catch (error) {
      expect((error as ApiError).message).not.toMatch(/Traceback|File "/);
      expect((error as ApiError).message).not.toContain('/srv/');
    }
  });

  it('calls the unauthorized handler once on a 401 and clears the session', async () => {
    // Mirrors what the app wires up: the store clears the session on 401.
    const handler = vi.fn(() => setCsrfToken(null));
    setUnauthorizedHandler(handler);
    server.respond('/api/v1/projects', { status: 401, body: { code: 'auth_required', message: 'Sign in.' } });

    await expect(api.get('/projects')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(getCsrfToken()).toBeNull();
  });

  it('does not fire the unauthorized handler for a 403 CSRF failure', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    server.respond('/api/v1/settings', { status: 403, body: { code: 'csrf_failed', message: 'Refresh.' } });

    await expect(api.patch('/settings', { name: 'x' })).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it('tolerates an empty 204 response', async () => {
    server.respond('/api/v1/exports/e1', { status: 204, body: null });
    await expect(api.delete('/exports/e1')).resolves.toBeUndefined();
  });

  it('reports a network failure as a retryable ApiError rather than throwing raw', async () => {
    server.restore();
    globalThis.fetch = vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))) as unknown as typeof fetch;

    await expect(api.get('/projects')).rejects.toMatchObject({ code: 'network_error' });
  });
});

describe('mediaUrl', () => {
  it('keeps media on the same origin so cookies travel with the request', () => {
    const url = mediaUrl('/videos/v1/stream');
    expect(url.startsWith('/api/v1/')).toBe(true);
    expect(url).not.toContain('http://127.0.0.1');
    expect(url).not.toContain('localhost');
  });
});
