/**
 * Live event client.
 *
 * Behaviour required by the product spec and implemented here:
 *  - automatic reconnect with exponential backoff and jitter
 *  - visible connection state (`connecting | open | reconnecting | closed`)
 *  - re-authentication after a reconnect (a fresh single-use ticket)
 *  - job resynchronisation on every (re)connect, so a job that finished while
 *    the socket was down is reflected without a page reload
 *  - heartbeats and staleness detection (a socket that goes quiet is recycled;
 *    browsers do not always tell us when a connection dies)
 */

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface ServerEvent {
  type: string;
  channel?: string;
  payload?: Record<string, unknown>;
  job_id?: string | null;
  project_id?: string | null;
  user_id?: string | null;
  ts?: number;
  event_id?: string;
}

type EventHandler = (event: ServerEvent) => void;
type StateHandler = (state: ConnectionState) => void;

const WS_PATH = '/ws/v1/events';
const BASE_DELAY = 800;
const MAX_DELAY = 20000;
const HEARTBEAT_INTERVAL = 20000;
const STALE_AFTER = 60000;

export interface LiveClientOptions {
  /** Fetches a single-use ticket; the session cookie alone cannot be replayed. */
  getTicket: () => Promise<string | null>;
  projectId?: string | null;
  onEvent: EventHandler;
  onState?: StateHandler;
  onResync?: (payload: Record<string, unknown>) => void;
}

export class LiveClient {
  private socket: WebSocket | null = null;
  private opening = false;
  private state: ConnectionState = 'idle';
  private attempt = 0;
  private closedByUser = false;
  private timers = new Set<number>();
  private lastMessageAt = 0;
  private readonly options: LiveClientOptions;

  constructor(options: LiveClientOptions) {
    this.options = options;
  }

  get currentState(): ConnectionState {
    return this.state;
  }

  connect(): void {
    this.closedByUser = false;
    // `opening` guards the window before `this.socket` is assigned, which
    // `open()` only does after awaiting a ticket. Without it, two calls in the
    // same tick (React StrictMode double-invokes effects) open two sockets and
    // every event is delivered twice.
    if (this.opening) return;
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    void this.open();
  }

  disconnect(): void {
    this.closedByUser = true;
    this.opening = false;
    this.clearTimers();
    if (this.socket) {
      try {
        this.socket.close(1000, 'client disconnect');
      } catch {
        /* already closing */
      }
      this.socket = null;
    }
    this.setState('closed');
  }

  /** Ask the server to re-send current job state (used after a refresh or error). */
  resync(): void {
    this.send({ action: 'resync', project_id: this.options.projectId ?? undefined });
  }

  subscribe(channels: string[]): void {
    this.send({ action: 'subscribe', channels });
  }

  private setState(state: ConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    this.options.onState?.(state);
  }

  private setTimeout(fn: () => void, delay: number): number {
    const id = window.setTimeout(() => {
      this.timers.delete(id);
      fn();
    }, delay);
    this.timers.add(id);
    return id;
  }

  private clearTimers(): void {
    this.timers.forEach((id) => window.clearTimeout(id));
    this.timers.clear();
  }

  private async open(): Promise<void> {
    if (this.opening) return;
    this.opening = true;
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');

    let ticket: string | null = null;
    try {
      ticket = await this.options.getTicket();
    } catch {
      ticket = null;
    }
    if (this.closedByUser) {
      this.opening = false;
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const query = new URLSearchParams();
    if (ticket) query.set('ticket', ticket);
    if (this.options.projectId) query.set('project_id', this.options.projectId);
    const url = `${protocol}://${window.location.host}${WS_PATH}${query.size ? `?${query}` : ''}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.opening = false;
      this.scheduleReconnect();
      return;
    }
    this.opening = false;
    this.socket = socket;
    this.lastMessageAt = Date.now();

    socket.onopen = () => {
      this.attempt = 0;
      this.setState('open');
      this.resync();
      this.startHeartbeat();
      if (this.options.projectId) this.subscribe([`project:${this.options.projectId}`]);
    };

    socket.onmessage = (raw) => {
      this.lastMessageAt = Date.now();
      let event: ServerEvent;
      try {
        event = JSON.parse(String(raw.data)) as ServerEvent;
      } catch {
        return;
      }
      if (event.type === 'system.resync') {
        this.options.onResync?.(event.payload || {});
      }
      if (event.type === 'system.error') {
        const code = (event.payload?.code as string) || '';
        if (code === 'not_authenticated') {
          // The socket could not authenticate: retry once with a fresh ticket.
          this.closedByUser = false;
          this.socket = null;
          this.scheduleReconnect(true);
          return;
        }
      }
      this.options.onEvent(event);
    };

    socket.onerror = () => {
      // `onclose` always follows; reconnect logic lives there.
    };

    socket.onclose = () => {
      this.socket = null;
      if (this.closedByUser) {
        this.setState('closed');
        return;
      }
      this.scheduleReconnect();
    };
  }

  private startHeartbeat(): void {
    const beat = () => {
      if (this.closedByUser) return;
      const stale = Date.now() - this.lastMessageAt > STALE_AFTER;
      if (this.socket?.readyState === WebSocket.OPEN) {
        if (stale) {
          // Silent socket: recycle it rather than showing stale progress forever.
          try {
            this.socket.close(4000, 'stale');
          } catch {
            /* ignore */
          }
          this.socket = null;
          this.scheduleReconnect();
          return;
        }
        this.send({ action: 'ping' });
      }
      this.setTimeout(beat, HEARTBEAT_INTERVAL);
    };
    this.setTimeout(beat, HEARTBEAT_INTERVAL);
  }

  private scheduleReconnect(immediate = false): void {
    if (this.closedByUser) return;
    this.attempt += 1;
    this.setState('reconnecting');
    const backoff = Math.min(MAX_DELAY, BASE_DELAY * 2 ** Math.min(this.attempt, 5));
    const jitter = Math.random() * backoff * 0.25;
    const delay = immediate ? 250 : backoff + jitter;
    this.setTimeout(() => void this.open(), delay);
  }

  private send(message: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }
}
