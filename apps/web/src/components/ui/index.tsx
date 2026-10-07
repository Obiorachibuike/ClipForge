import type { ReactNode } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/format';

export function Card({
  children,
  className,
  interactive,
}: {
  children: ReactNode;
  className?: string;
  interactive?: boolean;
}) {
  return <div className={cn('card p-5', interactive && 'card-hover', className)}>{children}</div>;
}

export function SectionHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 className="text-lg font-semibold text-slate-100">{title}</h2>
        {description ? <p className="mt-0.5 text-sm text-slate-400">{description}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-slate-400">
      <Loader2 className={cn('h-4 w-4 animate-spin', className)} aria-hidden />
      {label ? <span>{label}</span> : null}
    </span>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-card border border-dashed border-ink-600 bg-ink-900/40 px-6 py-14 text-center">
      {icon ? <div className="text-slate-500">{icon}</div> : null}
      <h3 className="text-base font-semibold text-slate-100">{title}</h3>
      {description ? <p className="max-w-md text-sm text-slate-400">{description}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="rounded-card border border-red-500/30 bg-red-500/[0.07] px-5 py-4">
      <p className="text-sm font-medium text-red-200">{message}</p>
      {onRetry ? (
        <button type="button" className="btn-secondary btn-sm mt-3" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton', className)} />;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-xl border border-ink-700/70 bg-ink-900/60 px-4 py-3">
      <p className="text-2xs font-semibold uppercase tracking-wider text-slate-500">{label}</p>
      <p className="mt-1 text-xl font-semibold text-slate-100">{value}</p>
      {hint ? <p className="mt-0.5 text-2xs text-slate-500">{hint}</p> : null}
    </div>
  );
}

export function ProgressBar({
  value,
  className,
  tone = 'accent',
}: {
  value: number;
  className?: string;
  tone?: 'accent' | 'success' | 'danger';
}) {
  const clamped = Math.max(0, Math.min(100, value));
  const tones = {
    accent: 'from-accent-500 to-violet-500',
    success: 'from-emerald-500 to-emerald-400',
    danger: 'from-red-500 to-red-400',
  } as const;
  return (
    <div className={cn('h-1.5 w-full overflow-hidden rounded-full bg-ink-700', className)}>
      <div
        className={cn('h-full rounded-full bg-gradient-to-r transition-[width] duration-500 ease-premium', tones[tone])}
        style={{ width: `${clamped}%` }}
        role="progressbar"
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
      />
    </div>
  );
}

export function StatusDot({ tone = 'neutral' }: { tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'accent' }) {
  const tones = {
    neutral: 'bg-ink-400',
    success: 'bg-emerald-400',
    warning: 'bg-amber-400',
    danger: 'bg-red-400',
    accent: 'bg-accent-400',
  } as const;
  return <span className={cn('inline-block h-2 w-2 shrink-0 rounded-full', tones[tone])} />;
}
