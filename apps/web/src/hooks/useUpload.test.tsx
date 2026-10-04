/**
 * `useUpload`: chunking, resume, cancellation and retry.
 *
 * The central guarantee under test is that the browser only ever holds a
 * `File` reference plus counters — a slice is sent per request, never the whole
 * video — and that a dropped connection resumes from the server's chunk list.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useUpload } from '@/hooks/useUpload';
import { useUiStore } from '@/stores/ui';
import { installFetchDouble, type FetchDouble } from '@/test/utils';

let server: FetchDouble;

const CHUNK_SIZE = 1024;

function makeFile(size = CHUNK_SIZE * 3, name = 'take.mp4', type = 'video/mp4'): File {
  return new File([new Uint8Array(size)], name, { type });
}

/** The body chunks the client actually transmitted, in order. */
function sentChunks(): number[] {
  return server.calls.filter((call) => /\/chunk\//.test(call.url)).map((call) => Number(call.url.split('/').pop()));
}

function chunkBodySizes(): number[] {
  return server.calls
    .filter((call) => /\/chunk\//.test(call.url))
    .map((call) => (call.body instanceof Blob ? call.body.size : 0));
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const initResponse = (overrides: Record<string, unknown> = {}) => ({
  body: {
    upload_id: 'up-1',
    project_id: 'p1',
    video_id: 'v1',
    chunk_size: CHUNK_SIZE,
    total_chunks: 3,
    received_chunks: [],
    expires_at: '2026-12-01T00:00:00Z',
    storage_backend: 'local',
    ...overrides,
  },
});

beforeEach(() => {
  server = installFetchDouble();
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
  server.respond('/uploads/init', initResponse());
  server.respond(/\/chunk\//, { status: 204, body: null });
  server.respond(/\/complete$/, { body: { video: { id: 'v1', original_filename: 'take.mp4' }, job: { id: 'j1', type: 'probe' } } });
  server.respond(/\/uploads\/up-1$/, { body: { upload_id: 'up-1', status: 'uploading', received_chunks: [], received_bytes: 0, total_chunks: 3, size_bytes: 3072, expires_at: '' } });
});

afterEach(() => {
  server.restore();
  vi.restoreAllMocks();
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
});

describe('useUpload', () => {
  it('splits the file into chunks and uploads them in order', async () => {
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      await result.current.start(makeFile());
    });

    expect(sentChunks()).toEqual([0, 1, 2]);
    // Every request body is one chunk — the whole file is never sent at once.
    expect(chunkBodySizes().every((size) => size <= CHUNK_SIZE)).toBe(true);
    expect(chunkBodySizes().reduce((sum, size) => sum + size, 0)).toBe(CHUNK_SIZE * 3);
  });

  it('declares the real filename, size and MIME type to the server', async () => {
    const { result } = renderHook(() => useUpload('p1'), { wrapper });
    await act(async () => {
      await result.current.start(makeFile(CHUNK_SIZE, 'interview.mp4', 'video/mp4'));
    });

    const init = server.calls.find((call) => call.url.includes('/uploads/init'));
    expect(init?.body).toMatchObject({
      filename: 'interview.mp4',
      size_bytes: CHUNK_SIZE,
      content_type: 'video/mp4',
      project_id: 'p1',
    });
  });

  it('reports progress and finishes with a success toast', async () => {
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      await result.current.start(makeFile());
    });

    await waitFor(() => expect(result.current.status).toBe('done'));
    expect(result.current.progress).toBe(100);
    expect(useUiStore.getState().toasts.some((toast) => toast.kind === 'success')).toBe(true);
  });

  it('never puts the File itself into state', async () => {
    const file = makeFile();
    const { result } = renderHook(() => useUpload('p1'), { wrapper });
    await act(async () => {
      await result.current.start(file);
    });

    // Only primitives are exposed; the reference stays in a ref.
    const exposed = JSON.stringify({
      status: result.current.status,
      progress: result.current.progress,
      message: result.current.message,
      filename: result.current.filename,
    });
    expect(exposed).not.toContain('Uint8Array');
    expect(Object.values(result.current)).not.toContain(file);
  });

  it('skips chunks the server already has', async () => {
    server.respond('/uploads/init', initResponse({ received_chunks: [0, 1] }));
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      await result.current.start(makeFile());
    });

    expect(sentChunks()).toEqual([2]);
  });

  it('retries a failing chunk instead of giving up immediately', async () => {
    let failures = 0;
    const original = globalThis.fetch;
    server.restore();
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (/\/chunk\//.test(url)) {
        failures += 1;
        if (failures < 3) throw new TypeError('connection reset');
        return new Response(null, { status: 204 });
      }
      return original(input, init);
    }) as unknown as typeof fetch;

    const { result } = renderHook(() => useUpload('p1'), { wrapper });
    await act(async () => {
      await result.current.start(makeFile(CHUNK_SIZE));
    });

    await waitFor(() => expect(result.current.status).toBe('done'));
    expect(failures).toBeGreaterThanOrEqual(3);
  });

  it('fails with the server message when a chunk keeps failing', async () => {
    server.respond(/\/chunk\//, { status: 500, body: { code: 'storage_error', message: 'Storage is unavailable.' } });
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      await result.current.start(makeFile(CHUNK_SIZE));
    });

    await waitFor(() => expect(result.current.status).toBe('failed'));
    expect(result.current.message).toBe('Storage is unavailable.');
    expect(useUiStore.getState().toasts.some((toast) => toast.kind === 'error')).toBe(true);
  });

  it('surfaces a rejected upload-init (extension or size guard)', async () => {
    server.respond('/uploads/init', {
      status: 422,
      body: { code: 'validation_error', message: 'That file type is not supported.', details: { field: 'filename' } },
    });
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      await result.current.start(makeFile(2048, 'script.sh', 'text/plain'));
    });

    await waitFor(() => expect(result.current.status).toBe('failed'));
    expect(result.current.message).toBe('That file type is not supported.');
    // Nothing was streamed: the guard rejected it before any chunk went out.
    expect(server.calls.some((call) => /\/chunk\//.test(call.url))).toBe(false);
  });

  it('resumes from the server chunk list after a failure', async () => {
    server.respond(/\/chunk\//, { status: 204, body: null });
    server.respond(/\/uploads\/up-1$/, {
      body: {
        upload_id: 'up-1',
        status: 'uploading',
        received_chunks: [0],
        received_bytes: CHUNK_SIZE,
        total_chunks: 3,
        size_bytes: CHUNK_SIZE * 3,
        expires_at: '',
      },
    });

    const { result } = renderHook(() => useUpload('p1'), { wrapper });
    await act(async () => {
      await result.current.start(makeFile());
    });
    expect(sentChunks()).toEqual([0, 1, 2]);

    server.calls.length = 0;
    server.respond(/\/uploads\/up-1$/, {
      body: {
        upload_id: 'up-1',
        status: 'uploading',
        received_chunks: [0, 1],
        received_bytes: CHUNK_SIZE * 2,
        total_chunks: 3,
        size_bytes: CHUNK_SIZE * 3,
        expires_at: '',
      },
    });

    await act(async () => {
      await result.current.resume();
    });

    expect(sentChunks()).toEqual([2]);
  });

  it('does nothing on resume when no upload has been started', async () => {
    const { result } = renderHook(() => useUpload('p1'), { wrapper });
    await act(async () => {
      await result.current.resume();
    });

    expect(result.current.status).toBe('idle');
    expect(server.calls).toHaveLength(0);
  });

  it('cancels an in-flight upload and tells the server to drop the session', async () => {
    server.respond(/\/chunk\//, { status: 204, body: null, delayMs: 25 });
    server.respond(/\/uploads\/up-1$/, { status: 204, body: null });
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    let pending: Promise<void> = Promise.resolve();
    await act(async () => {
      pending = result.current.start(makeFile());
      // Let init resolve and the first chunk start streaming, then cancel.
      await new Promise((resolve) => setTimeout(resolve, 10));
      await result.current.cancel();
    });
    await act(async () => {
      await pending;
    });

    expect(result.current.status).toBe('cancelled');
    expect(server.calls.some((call) => call.method === 'DELETE' && call.url.includes('/uploads/up-1'))).toBe(true);
    // The remaining chunks were never sent.
    expect(sentChunks().length).toBeLessThan(3);
  });

  it('aborting before the session exists sends no chunks at all', async () => {
    server.respond('/uploads/init', { ...initResponse(), delayMs: 20 });
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      const pending = result.current.start(makeFile());
      await result.current.cancel();
      await pending;
    });

    expect(result.current.status).toBe('cancelled');
    expect(sentChunks()).toEqual([]);
  });

  it('still reports cancellation if the server call fails', async () => {
    server.respond(/\/uploads\/up-1$/, { status: 500, body: { code: 'storage_error', message: 'nope' } });
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      const pending = result.current.start(makeFile());
      await result.current.cancel();
      await pending;
    });

    expect(result.current.status).toBe('cancelled');
  });

  it('reset returns the hook to a clean state', async () => {
    const { result } = renderHook(() => useUpload('p1'), { wrapper });
    await act(async () => {
      await result.current.start(makeFile());
    });
    expect(result.current.status).toBe('done');

    act(() => result.current.reset());

    expect(result.current.status).toBe('idle');
    expect(result.current.progress).toBe(0);
    expect(result.current.filename).toBe('');
  });

  it('uploads an empty file without dividing by zero', async () => {
    server.respond('/uploads/init', initResponse({ total_chunks: 0, received_chunks: [] }));
    const { result } = renderHook(() => useUpload('p1'), { wrapper });

    await act(async () => {
      await result.current.start(makeFile(0));
    });

    await waitFor(() => expect(result.current.status).toBe('done'));
    expect(sentChunks()).toEqual([]);
  });
});
