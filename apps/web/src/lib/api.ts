/**
 * Typed API client.
 *
 * Rules enforced here:
 *  - the session lives in an HttpOnly cookie: this code never touches tokens
 *    and never writes credentials to localStorage
 *  - non-GET requests send the CSRF header returned by /auth/session
 *  - every failure becomes an `ApiError` carrying the server's message, so the
 *    UI can show what actually went wrong instead of a generic "failed"
 *  - video bytes never pass through here: uploads stream in chunks
 */

export interface ApiErrorShape {
  code: string;
  message: string;
  details: Record<string, unknown>;
  request_id?: string;
}

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;
  requestId: string;

  constructor(status: number, body: Partial<ApiErrorShape> & { message?: string }) {
    super(body.message || `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code || 'request_failed';
    this.details = (body.details as Record<string, unknown>) || {};
    this.requestId = body.request_id || '';
  }

  get isAuth(): boolean {
    return this.status === 401;
  }

  get isRateLimited(): boolean {
    return this.status === 429;
  }

  get retryAfter(): number {
    const value = this.details?.retry_after;
    return typeof value === 'number' ? value : 0;
  }
}

const API_BASE = '/api/v1';

let csrfToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

type Query = Record<string, string | number | boolean | null | undefined>;

function buildUrl(path: string, query?: Query): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
  /** Skip CSRF for endpoints that authenticate by signature instead of session. */
  skipCsrf?: boolean;
  /** Return the raw Response (used by downloads and chunk uploads). */
  raw?: boolean;
  headers?: Record<string, string>;
}

async function parseError(response: Response): Promise<ApiError> {
  let body: Partial<ApiErrorShape> = {};
  try {
    body = (await response.json()) as Partial<ApiErrorShape>;
  } catch {
    body = { message: response.statusText || 'The request could not be completed.' };
  }
  if (response.headers.get('X-Session-Expired') === '1' && onUnauthorized) {
    onUnauthorized();
  } else if (response.status === 401 && onUnauthorized) {
    onUnauthorized();
  }
  if (response.status === 429) {
    const retry = Number(response.headers.get('Retry-After') || 0);
    body.details = { ...(body.details || {}), retry_after: retry };
  }
  return new ApiError(response.status, body);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, signal, skipCsrf, raw, headers } = options;
  const finalHeaders: Record<string, string> = { Accept: 'application/json', ...headers };
  if (body !== undefined && !(body instanceof FormData)) {
    finalHeaders['Content-Type'] = 'application/json';
  }
  if (method !== 'GET' && !skipCsrf && csrfToken) {
    finalHeaders['X-CSRF-Token'] = csrfToken;
  }

  const response = await fetch(buildUrl(path, query), {
    method,
    credentials: 'include',
    headers: finalHeaders,
    body:
      body === undefined
        ? undefined
        : body instanceof FormData
          ? body
          : JSON.stringify(body),
    signal,
  });

  if (!response.ok) throw await parseError(response);
  if (raw) return response as unknown as T;
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>(path, { method: 'POST', body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  put: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, { method: 'PUT', body, headers }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  raw: request,
};

/** Absolute URL for streaming endpoints (used by <video src>). */
export function mediaUrl(path: string): string {
  return `${API_BASE}${path}`;
}
