/** LiveClient: ticket auth, reconnect backoff, re-auth, resync and staleness. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveClient, type ConnectionState, type ServerEvent } from '@/ws/client';
import { FakeWebSocket, installFakeWebSocket } from '@/test/utils';

let restoreSocket: () => void;

beforeEach(() => {
  vi.useFakeTimers();
  restoreSocket = installFakeWebSocket();
});

afterEach(() => {
  restoreSocket();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function makeClient(overrides: Partial<ConstructorParameters<typeof LiveClient>[0]> = {}) {
  const events: ServerEvent[] = [];
  const states: ConnectionState[] = [];
  const resyncs: Array<Record<string, unknown>> = [];
  const getTicket = vi.fn(async () => 'ticket-1');

  const client = new LiveClient({
    getTicket,
    onEvent: (event) => events.push(event),
    onState: (state) => states.push(state),
    onResync: (payload) => resyncs.push(payload),
    ...overrides,
  });

  return { client, events, states, resyncs, getTicket };
}

describe('LiveClient', () => {
  it('fetches a ticket and connects to the versioned socket path', async () => {
    const { client } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    expect(FakeWebSocket.instances).toHaveLength(1);
    const socket = FakeWebSocket.last;
    expect(socket.url).toContain('/ws/v1/events');
    expect(socket.url).toContain('ticket=ticket-1');
  });

  it('requests a fresh ticket on every reconnect', async () => {
    const { client, getTicket } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    FakeWebSocket.last.serverClose();
    await vi.advanceTimersByTimeAsync(2000);
    expect(getTicket.mock.calls.length).toBeGreaterThanOrEqual(2);

    FakeWebSocket.last.open();
    expect(FakeWebSocket.last.url).toContain('ticket=ticket-1');
  });

  it('scopes the socket to the project when one is given', async () => {
    const { client } = makeClient({ projectId: 'proj-9' });
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    expect(FakeWebSocket.last.url).toContain('project_id=proj-9');
    const subscribe = FakeWebSocket.last.sent.map((frame) => JSON.parse(frame)).find((frame) => frame.action === 'subscribe');
    expect(subscribe?.channels).toContain('project:proj-9');
  });

  it('reports the connection lifecycle to the UI', async () => {
    const { client, states } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    expect(states[0]).toBe('connecting');
    expect(states).toContain('open');
  });

  it('asks the server to resync as soon as the socket opens', async () => {
    const { client } = makeClient({ projectId: 'p1' });
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    const resync = FakeWebSocket.last.sent.map((frame) => JSON.parse(frame)).find((frame) => frame.action === 'resync');
    expect(resync).toBeDefined();
    expect(resync.project_id).toBe('p1');
  });

  it('delivers parsed events and separates resync payloads', async () => {
    const { client, events, resyncs } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    FakeWebSocket.last.receive({ type: 'system.resync', payload: { jobs: [{ id: 'j1' }] } });
    FakeWebSocket.last.receive({ type: 'job.progress', job_id: 'j1', payload: { progress: 12 } });

    expect(resyncs).toHaveLength(1);
    expect(resyncs[0].jobs).toEqual([{ id: 'j1' }]);
    expect(events.map((event) => event.type)).toEqual(['system.resync', 'job.progress']);
  });

  it('ignores a frame that is not JSON instead of crashing', async () => {
    const { client, events } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    FakeWebSocket.last.receiveRaw('<html>gateway timeout</html>');
    FakeWebSocket.last.receive({ type: 'job.progress', payload: { progress: 5 } });

    expect(events.map((event) => event.type)).toEqual(['job.progress']);
  });

  it('backs off exponentially across repeated failed attempts', async () => {
    const { client } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    /** Milliseconds until the next attempt, given the socket dies again. */
    const measureGap = async (): Promise<number> => {
      const before = FakeWebSocket.instances.length;
      // Never opens — the connection keeps failing, so the attempt counter grows.
      FakeWebSocket.last.serverClose();
      let elapsed = 0;
      while (elapsed < 200_000) {
        await vi.advanceTimersByTimeAsync(50);
        elapsed += 50;
        if (FakeWebSocket.instances.length > before) return elapsed;
      }
      return Number.POSITIVE_INFINITY;
    };

    const first = await measureGap();
    const second = await measureGap();
    const third = await measureGap();

    // BASE_DELAY is 800 ms and doubles: 1.6 s, 3.2 s, 6.4 s (plus jitter ≤ 25%).
    expect(first).toBeGreaterThanOrEqual(1500);
    expect(first).toBeLessThanOrEqual(2200);
    expect(second).toBeGreaterThan(first + 1000);
    expect(third).toBeGreaterThan(second + 1000);
  });

  it('resets the backoff after a successful connection', async () => {
    const { client } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    // Two failed attempts push the delay up.
    FakeWebSocket.last.serverClose();
    await vi.advanceTimersByTimeAsync(3000);
    FakeWebSocket.last.serverClose();
    await vi.advanceTimersByTimeAsync(6000);

    FakeWebSocket.last.open();
    const before = FakeWebSocket.instances.length;
    FakeWebSocket.last.serverClose();
    let elapsed = 0;
    while (elapsed < 20_000 && FakeWebSocket.instances.length === before) {
      await vi.advanceTimersByTimeAsync(50);
      elapsed += 50;
    }

    // Back to the first step rather than the escalated delay.
    expect(elapsed).toBeLessThan(3000);
  });

  it('recycles a socket that goes silent instead of showing stale progress', async () => {
    const { client } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();
    const quiet = FakeWebSocket.last;

    // No traffic at all, but the socket stays "open" — the heartbeat must notice.
    await vi.advanceTimersByTimeAsync(120_000);

    expect(quiet.closeCalls.some((call) => call.code === 4000)).toBe(true);
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
  });

  it('keeps the socket alive while the server is answering pings', async () => {
    const { client } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();
    const socket = FakeWebSocket.last;

    for (let beat = 0; beat < 4; beat += 1) {
      await vi.advanceTimersByTimeAsync(30_000);
      socket.receive({ type: 'system.heartbeat', payload: { pong: true } });
    }

    expect(socket.closeCalls).toHaveLength(0);
    expect(socket.sent.filter((frame) => JSON.parse(frame).action === 'ping').length).toBeGreaterThan(0);
  });

  it('re-authenticates when the server rejects the ticket', async () => {
    const { client, getTicket } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    const before = getTicket.mock.calls.length;
    FakeWebSocket.last.receive({
      type: 'system.error',
      payload: { code: 'not_authenticated', message: 'Sign in to receive live updates.' },
    });
    await vi.advanceTimersByTimeAsync(1000);

    expect(getTicket.mock.calls.length).toBeGreaterThan(before);
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
  });

  it('stops reconnecting after an explicit disconnect', async () => {
    const { client, states } = makeClient();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last.open();

    const count = FakeWebSocket.instances.length;
    client.disconnect();
    await vi.advanceTimersByTimeAsync(120_000);

    expect(FakeWebSocket.instances).toHaveLength(count);
    expect(states[states.length - 1]).toBe('closed');
  });

  it('does not open a second socket while one is connecting', async () => {
    const { client } = makeClient();
    client.connect();
    client.connect();
    await vi.advanceTimersByTimeAsync(0);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('reconnects if the constructor throws', async () => {
    const { client } = makeClient();
    const RealWebSocket = globalThis.WebSocket;
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = function Throwing() {
      throw new Error('blocked by policy');
    };
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    (globalThis as unknown as { WebSocket: unknown }).WebSocket = RealWebSocket;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeWebSocket.instances.length).toBeGreaterThan(0);
  });

  it('still connects when the ticket endpoint fails', async () => {
    const { client } = makeClient({ getTicket: vi.fn(async () => null) });
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    expect(FakeWebSocket.instances).toHaveLength(1);
    // No ticket in the URL: the HttpOnly session cookie does the authenticating.
    expect(FakeWebSocket.last.url).not.toContain('ticket=');
  });

  it('survives a rejected getTicket promise', async () => {
    const { client } = makeClient({
      getTicket: vi.fn(async () => {
        throw new Error('network down');
      }),
    });
    client.connect();
    await vi.advanceTimersByTimeAsync(0);

    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
