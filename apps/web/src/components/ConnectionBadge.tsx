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
  idle: 'text-slate-500',
  connecting: 'text-amber-400 animate-pulse',
  open: 'text-emerald-400',
  reconnecting: 'text-amber-400 animate-pulse',
  closed: 'text-red-400',
};

function SignalIcon({ state }: { state: ConnectionState }) {
  return (
    <svg viewBox="0 0 20 20" className={cn('h-3.5 w-3.5', TONES[state])} fill="none" aria-hidden>
      <circle cx="10" cy="14.8" r="1.55" fill="currentColor" />
      <path d="M6.65 11.5a4.75 4.75 0 0 1 6.7 0" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" />
      <path d="M3.95 8.75a8.6 8.6 0 0 1 12.1 0" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" opacity=".72" />
    </svg>
  );
}

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
      <SignalIcon state={state} />
      {LABELS[state]}
    </span>
  );
}
