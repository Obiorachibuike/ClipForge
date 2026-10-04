import { cn } from '@/lib/format';
import type { ConnectionState } from '@/ws/client';

const LABELS: Record<ConnectionState, string> = {
  idle: 'Idle',
  connecting: 'Connecting',
  open: 'Live',
  reconnecting: 'Reconnecting',
  closed: 'Offline',
};

const TONES: Record<ConnectionState, string> = {
  idle: 'bg-ink-500',
  connecting: 'bg-amber-400 animate-pulse',
  open: 'bg-emerald-400',
  reconnecting: 'bg-amber-400 animate-pulse',
  closed: 'bg-red-400',
};

/**
 * Honest connection indicator. When the socket is down the app keeps working
 * from REST and says so, instead of showing a fake "live" badge.
 */
export default function ConnectionBadge({ state, className }: { state: ConnectionState; className?: string }) {
  const degraded = state === 'closed' || state === 'reconnecting';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-2 rounded-pill border px-2.5 py-1 text-2xs font-semibold',
        degraded ? 'border-amber-500/30 bg-amber-500/10 text-amber-200' : 'border-ink-600 bg-ink-800 text-slate-300',
        className,
      )}
      title={
        degraded
          ? 'Live updates are reconnecting. Data is still refreshed over the API.'
          : 'Live updates are connected'
      }
    >
      <span className="relative flex h-1.5 w-1.5">
        <span className={cn('h-1.5 w-1.5 rounded-full', TONES[state])} />
        {state === 'open' ? (
          <span className="absolute inset-0 animate-pulseRing rounded-full bg-emerald-400/70" />
        ) : null}
      </span>
      {LABELS[state]}
    </span>
  );
}
