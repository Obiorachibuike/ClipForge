import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, Cpu, KeyRound, Lock, ShieldCheck, Trash2, XCircle } from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { formatBytes, formatDateTime, titleCase } from '@/lib/format';
import { Card, SectionHeader, Skeleton, Stat } from '@/components/ui';
import { toast } from '@/stores/ui';
import type { AIProvider, Capabilities, Plan, UsageSummary } from '@/types/api';

interface SettingsResponse {
  user: {
    id: string;
    email: string;
    name: string;
    plan: string;
    privacy_mode: string;
    default_aspect_ratio: string;
    default_caption_preset: string;
  };
  preferences: Record<string, unknown>;
  privacy: {
    mode: string;
    modes: Array<{ key: string; label: string; detail: string }>;
  };
  capabilities: Capabilities;
}

function CapabilityRow({
  label,
  ok,
  detail,
}: {
  label: string;
  ok: boolean;
  detail?: string;
}) {
  return (
    <div className="flex items-start gap-3 border-b border-ink-700/60 py-2.5 last:border-0">
      {ok ? (
        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" aria-hidden />
      ) : (
        <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" aria-hidden />
      )}
      <div className="min-w-0">
        <p className="text-sm text-slate-200">{label}</p>
        {detail ? <p className="mt-0.5 text-xs text-slate-500">{detail}</p> : null}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [providerKey, setProviderKey] = useState('');
  const [providerBase, setProviderBase] = useState('');
  const [providerModel, setProviderModel] = useState('');

  const settings = useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => api.get<SettingsResponse>('/settings'),
  });

  const usage = useQuery({
    queryKey: queryKeys.usage,
    queryFn: () => api.get<UsageSummary>('/usage'),
  });

  const plans = useQuery({
    queryKey: queryKeys.plans,
    queryFn: () => api.get<{ plans: Plan[]; providers: string[] }>('/billing/plans'),
    staleTime: 10 * 60_000,
  });

  const providers = useQuery({
    queryKey: queryKeys.providers,
    queryFn: () => api.get<AIProvider[]>('/settings/ai-providers'),
  });

  const saveProfile = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.patch<{ name: string }>('/settings', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
      toast({ kind: 'success', title: 'Settings saved' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not save settings', description: (error as Error).message }),
  });

  const addProvider = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.post<AIProvider>('/settings/ai-providers', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.providers });
      queryClient.invalidateQueries({ queryKey: queryKeys.capabilities });
      setProviderKey('');
      setProviderBase('');
      setProviderModel('');
      toast({ kind: 'success', title: 'Provider added', description: 'The key is stored encrypted.' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not add the provider', description: (error as Error).message }),
  });

  const testProvider = useMutation({
    mutationFn: (id: string) => api.post<{ ok: boolean; message: string }>(`/settings/ai-providers/${id}/test`),
    onSuccess: (result) =>
      toast({
        kind: result.ok ? 'success' : 'warning',
        title: result.ok ? 'Provider responded' : 'Provider test failed',
        description: result.message,
      }),
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Test failed', description: (error as Error).message }),
  });

  const data = settings.data;

  if (settings.isPending) {
    return (
      <div className="mx-auto max-w-4xl space-y-6">
        <Skeleton className="h-8 w-1/4" />
        {[0, 1, 2].map((index) => (
          <Skeleton key={index} className="h-40" />
        ))}
      </div>
    );
  }

  const caps = data?.capabilities;
  const metrics = usage.data?.metrics ?? {};

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-white">Settings</h1>
        <p className="mt-1 text-sm text-slate-400">Profile, privacy, AI providers, usage and deployment capabilities.</p>
      </div>

      <Card>
        <SectionHeader title="Profile" description={data?.user.email} />
        <form
          className="grid gap-4 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            const payload: Record<string, unknown> = {};
            if (name.trim()) payload.name = name.trim();
            saveProfile.mutate(payload);
          }}
        >
          <div>
            <label className="label" htmlFor="settings-name">
              Display name
            </label>
            <input
              id="settings-name"
              className="input"
              placeholder={data?.user.name || 'Your name'}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="settings-aspect">
              Default format
            </label>
            <select
              id="settings-aspect"
              className="input"
              value={data?.user.default_aspect_ratio}
              onChange={(event) => saveProfile.mutate({ default_aspect_ratio: event.target.value })}
            >
              <option value="9:16">Vertical 9:16</option>
              <option value="1:1">Square 1:1</option>
              <option value="16:9">Landscape 16:9</option>
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="label" htmlFor="settings-preset">
              Default caption preset
            </label>
            <select
              id="settings-preset"
              className="input"
              value={data?.user.default_caption_preset}
              onChange={(event) => saveProfile.mutate({ default_caption_preset: event.target.value })}
            >
              {['bold', 'classic', 'minimal', 'podcast', 'karaoke', 'creator', 'high_contrast'].map((preset) => (
                <option key={preset} value={preset}>
                  {titleCase(preset)}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-2 flex justify-end">
            <button type="submit" className="btn-primary btn-sm" disabled={saveProfile.isPending}>
              Save profile
            </button>
          </div>
        </form>
      </Card>

      <Card>
        <SectionHeader
          title="Privacy"
          description="Where your media and transcripts are processed. Stated plainly, because it matters."
        />
        <div className="space-y-3">
          {data?.privacy.modes.map((mode) => (
            <button
              key={mode.key}
              type="button"
              onClick={() => saveProfile.mutate({ privacy_mode: mode.key })}
              className={`w-full rounded-xl border px-4 py-3 text-left transition-colors ${
                data.privacy.mode === mode.key
                  ? 'border-accent-500/50 bg-accent-500/[0.08]'
                  : 'border-ink-700 bg-ink-900/50 hover:border-ink-600'
              }`}
            >
              <p className="flex items-center gap-2 text-sm font-medium text-white">
                {mode.key === 'private_ai' ? (
                  <Lock className="h-3.5 w-3.5 text-accent-300" aria-hidden />
                ) : (
                  <ShieldCheck className="h-3.5 w-3.5 text-slate-400" aria-hidden />
                )}
                {mode.label}
                {data.privacy.mode === mode.key ? <span className="badge-accent">active</span> : null}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-slate-400">{mode.detail}</p>
            </button>
          ))}
        </div>
      </Card>

      <Card>
        <SectionHeader
          title="AI providers"
          description="Bring your own keys. Keys are encrypted at rest and never returned to the browser."
          action={
            <Link to="/pricing" className="btn-ghost btn-sm">
              Compare plans
            </Link>
          }
        />

        {providers.data && providers.data.length > 0 ? (
          <div className="mb-5 space-y-2">
            {providers.data.map((provider) => (
              <div
                key={provider.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-200">
                    {provider.label || provider.provider} · {provider.kind}
                  </p>
                  <p className="mt-0.5 flex items-center gap-2 text-xs text-slate-500">
                    <KeyRound className="h-3 w-3" aria-hidden />
                    {provider.masked_api_key || (provider.has_api_key ? 'key stored' : 'no key')}
                    {provider.model ? <span>· {provider.model}</span> : null}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    onClick={() => testProvider.mutate(provider.id)}
                    disabled={testProvider.isPending}
                  >
                    Test
                  </button>
                  <button
                    type="button"
                    className="btn-ghost btn-sm text-red-300"
                    onClick={async () => {
                      try {
                        await api.delete(`/settings/ai-providers/${provider.id}`);
                        queryClient.invalidateQueries({ queryKey: queryKeys.providers });
                        toast({ kind: 'success', title: 'Provider removed' });
                      } catch (error) {
                        toast({ kind: 'error', title: 'Could not remove it', description: (error as Error).message });
                      }
                    }}
                    aria-label="Remove provider"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="mb-5 text-sm text-slate-500">
            No personal providers configured — the deployment defaults are used.
          </p>
        )}

        <form
          className="grid gap-4 rounded-xl border border-ink-700/70 bg-ink-900/40 p-4 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            addProvider.mutate({
              provider: 'openai_compatible',
              kind: 'llm',
              label: 'Custom',
              api_key: providerKey,
              base_url: providerBase || undefined,
              model: providerModel || undefined,
            });
          }}
        >
          <div>
            <label className="label" htmlFor="provider-key">
              API key
            </label>
            <input
              id="provider-key"
              type="password"
              className="input"
              value={providerKey}
              onChange={(event) => setProviderKey(event.target.value)}
              placeholder="sk-…"
              autoComplete="off"
            />
          </div>
          <div>
            <label className="label" htmlFor="provider-model">
              Model
            </label>
            <input
              id="provider-model"
              className="input"
              value={providerModel}
              onChange={(event) => setProviderModel(event.target.value)}
              placeholder="gpt-4o-mini"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label" htmlFor="provider-base">
              Base URL (optional, for OpenAI-compatible endpoints)
            </label>
            <input
              id="provider-base"
              className="input"
              value={providerBase}
              onChange={(event) => setProviderBase(event.target.value)}
              placeholder="https://api.openai.com/v1"
            />
          </div>
          <div className="sm:col-span-2 flex justify-end">
            <button type="submit" className="btn-primary btn-sm" disabled={!providerKey || addProvider.isPending}>
              Add provider
            </button>
          </div>
        </form>
      </Card>

      <Card>
        <SectionHeader title="Usage this period" description={usage.data?.plan ? `${titleCase(usage.data.plan)} plan` : undefined} />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(metrics).map(([key, metric]) => (
            <Stat
              key={key}
              label={titleCase(key)}
              value={
                key === 'storage_used'
                  ? formatBytes(metric.used)
                  : metric.used >= 1000
                    ? Math.round(metric.used).toLocaleString()
                    : metric.used.toFixed(metric.used < 10 ? 1 : 0)
              }
              hint={metric.limit ? `of ${metric.limit >= 1000 ? Math.round(metric.limit).toLocaleString() : metric.limit}` : 'unlimited'}
            />
          ))}
        </div>
        {plans.data ? (
          <p className="mt-4 text-xs text-slate-500">
            Limits come from your plan: {plans.data.plans.find((plan) => plan.key === usage.data?.plan)?.name ?? 'Free'}.
          </p>
        ) : null}
      </Card>

      <Card>
        <SectionHeader
          title="Deployment capabilities"
          description="What this server can actually run. Absent features are reported as absent."
        />
        {caps ? (
          <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">
            <div>
              <CapabilityRow label="FFmpeg media pipeline" ok={caps.media_pipeline} />
              <CapabilityRow label="FFprobe" ok={caps.ffprobe} detail={caps.ffprobe ? undefined : 'Using PyAV probing'} />
              <CapabilityRow
                label="Face detection / smart framing"
                ok={caps.vision.face_detection}
                detail={`detector: ${caps.vision.active}`}
              />
              <CapabilityRow
                label="Local transcription (faster-whisper)"
                ok={caps.transcription.local.available}
                detail={caps.transcription.local.detail || `model ${caps.transcription.local.model}`}
              />
            </div>
            <div>
              <CapabilityRow
                label="Script alignment (exact word timings)"
                ok={Boolean(caps.transcription.script_alignment?.available)}
                detail={caps.transcription.script_alignment?.detail}
              />
              <CapabilityRow
                label="API transcription"
                ok={caps.transcription.api.available}
                detail={caps.transcription.api.model}
              />
              <CapabilityRow
                label="Language model (headlines, narration)"
                ok={caps.llm.configured.length > 0}
                detail={caps.llm.configured.length ? caps.llm.configured.join(', ') : 'structural scoring only'}
              />
              <CapabilityRow
                label="Neural embeddings"
                ok={caps.embeddings.neural_available}
                detail={caps.embeddings.note}
              />
            </div>
            <div className="sm:col-span-2 mt-3 grid gap-3 sm:grid-cols-4">
              <Stat label="Storage" value={titleCase(caps.storage_backend)} />
              <Stat label="Queue" value={titleCase(caps.queue_backend)} />
              <Stat label="Environment" value={titleCase(caps.environment)} />
              <Stat
                label="Rust processor"
                value={caps.processor.enabled ? 'enabled' : 'off'}
                hint={caps.processor.enabled ? caps.processor.url : 'Python pipeline in use'}
              />
            </div>
          </div>
        ) : null}

        {!caps?.media_pipeline ? (
          <p className="mt-4 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/[0.07] px-4 py-3 text-sm text-amber-200">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            FFmpeg is not available, so media jobs will fail. Set MEDIA_FFMPEG_PATH or install imageio-ffmpeg.
          </p>
        ) : null}
      </Card>

      <Card>
        <SectionHeader title="Subscription" />
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="flex items-center gap-2 text-sm text-slate-200">
              <Cpu className="h-4 w-4 text-slate-500" aria-hidden />
              {plans.data?.plans.find((plan) => plan.key === usage.data?.plan)?.name ?? 'Free'} plan
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Account created {formatDateTime(data?.user.id ? undefined : undefined) || 'recently'}.
            </p>
          </div>
          <Link to="/pricing" className="btn-secondary btn-sm">
            View plans
          </Link>
        </div>
      </Card>
    </div>
  );
}
