import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Check } from 'lucide-react';
import { api } from '@/lib/api';
import ClipForgeMark from '@/components/ClipForgeMark';
import ThemeToggle from '@/components/ThemeToggle';
import { queryKeys } from '@/lib/query';
import { useAuthStore } from '@/stores/auth';
import { cn, formatBytes } from '@/lib/format';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import type { Plan } from '@/types/api';

export default function PricingPage() {
  const status = useAuthStore((state) => state.status);
  const plans = useQuery({
    queryKey: queryKeys.plans,
    queryFn: () => api.get<{ plans: Plan[]; providers: string[] }>('/billing/plans'),
    staleTime: 10 * 60_000,
  });

  return (
    <div className="min-h-screen bg-ink-950">
      <header className="border-b border-ink-800">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
          <Link to="/" className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-accent-500 to-violet-500 shadow-[0_8px_24px_rgba(91,108,255,0.3)]">
              <ClipForgeMark className="h-5 w-5 text-white" />
            </span>
            <span className="text-base font-semibold tracking-tight text-slate-100">ClipForge</span>
          </Link>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <Link to={status === 'authenticated' ? '/dashboard' : '/login'} className="btn-secondary btn-sm">
              {status === 'authenticated' ? 'Dashboard' : 'Sign in'}
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-100">Simple pricing</h1>
        <p className="mt-3 max-w-2xl text-slate-400">
          Every plan runs the same pipeline. Higher tiers raise the processing minutes, render count and storage.
        </p>

        {plans.isPending ? (
          <div className="mt-10 grid gap-6 md:grid-cols-3">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} className="h-96" />
            ))}
          </div>
        ) : plans.isError ? (
          <div className="mt-10">
            <ErrorState
              message={plans.error instanceof Error ? plans.error.message : 'Could not load plans.'}
              onRetry={() => void plans.refetch()}
            />
          </div>
        ) : (
          <>
            <div className="mt-10 grid gap-6 md:grid-cols-3">
              {plans.data?.plans.map((plan) => (
                <Card
                  key={plan.key}
                  className={cn('flex flex-col p-6', plan.highlighted && 'border-accent-500/40 shadow-glow')}
                >
                  {plan.highlighted ? <span className="badge-accent mb-3 w-fit">Recommended</span> : null}
                  <h2 className="text-lg font-semibold text-slate-100">{plan.name}</h2>
                  <p className="mt-1 text-sm text-slate-400">{plan.tagline}</p>
                  <p className="mt-5 text-3xl font-semibold text-slate-100">
                    {plan.price_minor === 0 ? 'Free' : `$${(plan.price_minor / 100).toFixed(0)}`}
                    {plan.price_minor > 0 ? (
                      <span className="text-sm font-normal text-slate-500">
                        {plan.interval === 'once' ? ' one time' : ' / month'}
                      </span>
                    ) : null}
                  </p>

                  <ul className="mt-6 flex-1 space-y-2.5">
                    {plan.features.map((feature) => (
                      <li key={feature} className="flex gap-2.5 text-sm text-slate-300">
                        <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" aria-hidden />
                        {feature}
                      </li>
                    ))}
                  </ul>

                  <dl className="mt-6 space-y-1.5 border-t border-ink-700 pt-4 text-xs text-slate-400">
                    <div className="flex justify-between">
                      <dt>Minutes processed</dt>
                      <dd className="tabular-nums">{Math.round(plan.limits.minutes_processed)}</dd>
                    </div>
                    <div className="flex justify-between">
                      <dt>Videos</dt>
                      <dd className="tabular-nums">{plan.limits.videos_uploaded}</dd>
                    </div>
                    <div className="flex justify-between">
                      <dt>Clips</dt>
                      <dd className="tabular-nums">{plan.limits.clips_generated}</dd>
                    </div>
                    <div className="flex justify-between">
                      <dt>Renders</dt>
                      <dd className="tabular-nums">{plan.limits.renders}</dd>
                    </div>
                    <div className="flex justify-between">
                      <dt>Storage</dt>
                      <dd className="tabular-nums">{formatBytes(plan.limits.storage_bytes)}</dd>
                    </div>
                    <div className="flex justify-between">
                      <dt>Max upload</dt>
                      <dd className="tabular-nums">{formatBytes(plan.limits.max_upload_bytes)}</dd>
                    </div>
                  </dl>

                  <Link
                    to={status === 'authenticated' ? '/settings' : '/register'}
                    className={cn('mt-6', plan.highlighted ? 'btn-primary' : 'btn-secondary')}
                  >
                    {plan.current
                      ? 'Current plan'
                      : plan.price_minor === 0
                        ? 'Start free'
                        : `Upgrade to ${plan.name}`}
                  </Link>
                </Card>
              ))}
            </div>

            {plans.data && plans.data.providers.length === 0 ? (
              <p className="mt-8 rounded-card border border-ink-700/70 bg-ink-900/50 px-4 py-3 text-sm text-slate-400">
                Online checkout is not configured on this deployment yet. Paid upgrades are activated by an
                administrator; the Free plan works immediately.
              </p>
            ) : null}
          </>
        )}
      </main>
    </div>
  );
}
