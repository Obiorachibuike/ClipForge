/**
 * Test harness: renders components with the same providers the real app uses
 * (router + TanStack Query) and offers a small fetch double so API and WebSocket
 * tests can script exact server responses.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement, ReactNode } from 'react';

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

interface Options extends Omit<RenderOptions, 'wrapper'> {
  route?: string;
  queryClient?: QueryClient;
}

export function renderWithProviders(ui: ReactElement, options: Options = {}) {
  const { route = '/', queryClient = createTestQueryClient(), ...rest } = options;

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }

  return { queryClient, ...render(ui, { wrapper: Wrapper, ...rest }) };
}

// --------------------------------------------------------------- fetch -----
export interface ScriptedResponse {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Milliseconds to wait before answering, to observe in-flight UI state. */
  delayMs?: number;
}

export interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

export interface FetchDouble {
  calls: RecordedCall[];
  queue: (response: ScriptedResponse) => void;
  respond: (matcher: string | RegExp, response: ScriptedResponse) => void;
  restore: () => void;
}

/**
 * Installs a `fetch` double. Responses are matched in order: `respond` handlers
 * keyed by URL wins over the FIFO `queue`.
 */
export function installFetchDouble(): FetchDouble {
  const calls: RecordedCall[] = [];
  const handlers: Array<{ matcher: string | RegExp; response: ScriptedResponse }> = [];
  const queue: ScriptedResponse[] = [];
  const original = globalThis.fetch;

  const matches = (matcher: string | RegExp, url: string) =>
    typeof matcher === 'string' ? url.includes(matcher) : matcher.test(url);

  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    let body: unknown = init?.body ?? null;
    if (typeof body === 'string') {
      try {
        body = JSON.parse(body);
      } catch {
        /* leave as text */
      }
    }
    const headers: Record<string, string> = {};
    new Headers(init?.headers ?? {}).forEach((value, key) => {
      headers[key] = value;
    });
    calls.push({ url, method, body, headers });

    const handlerIndex = handlers.findIndex((entry) => matches(entry.matcher, url));
    const scripted = handlerIndex >= 0 ? handlers[handlerIndex].response : (queue.shift() ?? { status: 200, body: {} });
    if (scripted.delayMs) {
      await new Promise((resolve) => setTimeout(resolve, scripted.delayMs));
    }
    const status = scripted.status ?? 200;
    const payload = scripted.body ?? {};

    return new Response(status === 204 ? null : JSON.stringify(payload), {
      status,
      headers: { 'Content-Type': 'application/json', ...(scripted.headers ?? {}) },
    });
  }) as typeof fetch;

  return {
    calls,
    queue: (response) => queue.push(response),
    respond: (matcher, response) => handlers.unshift({ matcher, response }),
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

// --------------------------------------------------------------- socket ----
interface ListenerMap {
  open: Array<() => void>;
  message: Array<(event: MessageEvent) => void>;
  close: Array<(event: CloseEvent) => void>;
  error: Array<() => void>;
}

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  closeCalls: Array<{ code?: number; reason?: string }> = [];

  // Both WebSocket handler styles are supported because the app uses the
  // property form (`socket.onopen = ...`) while adding a listener is equally
  // valid — the fake must not decide which one the client prefers.
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  private listeners: ListenerMap = { open: [], message: [], close: [], error: [] };

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: keyof ListenerMap, handler: never): void {
    (this.listeners[type] as Array<unknown>).push(handler);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
    this.readyState = FakeWebSocket.CLOSED;
    this.emit('close', { code, reason } as CloseEvent);
  }

  // -- test helpers ------------------------------------------------------
  emit(type: keyof ListenerMap, event: unknown): void {
    const property = { open: 'onopen', message: 'onmessage', close: 'onclose', error: 'onerror' }[type] as
      | 'onopen'
      | 'onmessage'
      | 'onclose'
      | 'onerror';
    const direct = this[property] as ((payload: unknown) => void) | null;
    if (typeof direct === 'function') direct(event);
    (this.listeners[type] as Array<(payload: unknown) => void>).forEach((handler) => handler(event));
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.emit('open', {});
  }

  receive(payload: unknown): void {
    this.emit('message', { data: JSON.stringify(payload) } as MessageEvent);
  }

  receiveRaw(data: string): void {
    this.emit('message', { data } as MessageEvent);
  }

  serverClose(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.emit('close', { code: 1006 } as CloseEvent);
  }

  static reset(): void {
    FakeWebSocket.instances = [];
  }

  static get last(): FakeWebSocket {
    const instance = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    if (!instance) throw new Error('no WebSocket has been constructed');
    return instance;
  }
}

export function installFakeWebSocket(): () => void {
  const original = globalThis.WebSocket;
  FakeWebSocket.reset();
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket;
  return () => {
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = original;
    FakeWebSocket.reset();
  };
}

export const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
