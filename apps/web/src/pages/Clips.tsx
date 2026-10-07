import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Lightbulb, Play, Scissors, Sparkles, Trash2, X } from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useLive } from '@/lib/live';
import { cn, formatDuration, formatTime } from '@/lib/format';
import { Card, EmptyState, ErrorState, ProgressBar, SectionHeader, Skeleton, StatusDot } from '@/components/ui';
import { toast } from '@/stores/ui';
import type { Candidate, Job, Page } from '@/types/api';

const CATEGORY_TONES: Record<string, string> = {
  insight: 'bg-accent-500/15 text-accent-200',
  story: 'bg-violet-500/15 text-violet-300',
  hook: 'bg-emerald-500/15 text-emerald-300',
  tutorial: 'bg-sky-500/15 text-sky-300',
};

function ScoreBar({ label, value }: { label: string; value: number }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div>
      <div className="flex items-center justify-between text-2xs">
        <span className="capitalize text-slate-400">{label}</span>
        <span className="tabular-nums text-slate-300">{Math.round(clamped)}</span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-ink-700">
        <div
          className={cn(
            'h-full rounded-full',
            clamped >= 70 ? 'bg-emerald-400' : clamped >= 45 ? 'bg-accent-400' : 'bg-ink-500',
          )}
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}

function CandidateCard({
  candidate,
  videoId,
  onKeep,
  onReject,
  onMakeClip,
  busy,
}: {
  candidate: Candidate;
  videoId: string | null;
  onKeep: () => void;
  onReject: () => void;
  onMakeClip: () => void;
  busy: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const previewRef = useRef<HTMLVideoElement>(null);

  const preview = () => {
    const element = previewRef.current;
    if (!element || !videoId) return;
    element.currentTime = candidate.start_time;
    void element.play();
    element.onended = () => element.pause();
    const stopAt = candidate.end_time;
    const check = () => {
      if (element.currentTime >= stopAt) {
        element.pause();
        element.removeEventListener('timeupdate', check);
      }
    };
    element.addEventListener('timeupdate', check);
  };

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="badge-accent">{candidate.score}</span>
            <span className={cn('badge', CATEGORY_TONES[candidate.category] || 'badge-neutral')}>
              {candidate.category}
            </span>
            <span className="font-mono text-2xs text-slate-500">
              {formatTime(candidate.start_time)} → {formatTime(candidate.end_time)} ·{' '}
              {formatDuration(candidate.duration)}
            </span>
            {candidate.speaker ? <span className="badge-neutral">{candidate.speaker}</span> : null}
            {candidate.status !== 'pending' ? (
              <span className="badge-neutral inline-flex items-center gap-1.5">
                <StatusDot tone={candidate.status === 'rejected' ? 'danger' : 'success'} />
                {candidate.status}
              </span>
            ) : null}
          </div>
          <h3 className="mt-2 text-base font-semibold text-slate-100">{candidate.title}</h3>
          <p className="mt-1 text-sm text-slate-400">{candidate.hook || candidate.reason}</p>
        </div>
      </div>

      {videoId ? (
        <div className="overflow-hidden rounded-xl border border-ink-700 bg-black">
          <video
            ref={previewRef}
            className="aspect-video w-full"
            preload="metadata"
            src={`/api/v1/videos/${videoId}/stream#t=${candidate.start_time}`}
            controls
          />
        </div>
      ) : null}

      <div className="rounded-xl border border-ink-700/70 bg-ink-900/50 p-3.5">
        <p className="text-sm leading-relaxed text-slate-300">{candidate.transcript_text}</p>
      </div>

      {expanded ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {(Object.entries(candidate.scores) as Array<[string, number]>)
            .filter(([key]) => key !== 'structural')
            .map(([key, value]) => (
              <ScoreBar key={key} label={key} value={value} />
            ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn-secondary btn-sm" onClick={preview}>
          <Play className="h-3.5 w-3.5" aria-hidden />
          Play moment
        </button>
        <button type="button" className="btn-ghost btn-sm" onClick={() => setExpanded((value) => !value)}>
          <Lightbulb className="h-3.5 w-3.5" aria-hidden />
          {expanded ? 'Hide scoring' : 'Why this score'}
        </button>
        <div className="flex-1" />
        <button
          type="button"
          className="btn-ghost btn-sm text-red-300 hover:bg-red-500/10"
          onClick={onReject}
          disabled={busy}
        >
          <X className="h-3.5 w-3.5" aria-hidden />
          Reject
        </button>
        <button type="button" className="btn-secondary btn-sm" onClick={onKeep} disabled={busy}>
          <Check className="h-3.5 w-3.5" aria-hidden />
          Keep
        </button>
        <button type="button" className="btn-primary btn-sm" onClick={onMakeClip} disabled={busy}>
          <Scissors className="h-3.5 w-3.5" aria-hidden />
          Create clip
        </button>
      </div>
    </Card>
  );
}

export default function ClipsPage() {
  const { projectId = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { connection } = useLive();
  const [filter, setFilter] = useState<'all' | 'pending' | 'kept' | 'rejected'>('all');

  const project = useQuery({
    queryKey: queryKeys.project(projectId),
    queryFn: () => api.get<{ latest_video: { id: string } | null; clip_count: number }>(`/projects/${projectId}`),
  });

  const candidates = useQuery({
    queryKey: queryKeys.candidates(projectId, { filter }),
    queryFn: () =>
      api.get<Page<Candidate>>(`/projects/${projectId}/candidates`, {
        limit: 50,
        status: filter === 'all' ? undefined : filter,
      }),
    refetchInterval: connection === 'open' ? false : 6000,
  });

  const clips = useQuery({
    queryKey: queryKeys.clips(projectId),
    queryFn: () => api.get<Page<{ id: string; title: string; duration: number; status: string }>>(
      `/projects/${projectId}/clips`,
      { limit: 50 },
    ),
  });

  const discover = useMutation({
    mutationFn: () => api.post<Job>(`/projects/${projectId}/discover`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projectJobs(projectId) });
      toast({ kind: 'info', title: 'Clip discovery queued' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not start discovery', description: (error as Error).message }),
  });

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api.patch<Candidate>(`/candidates/${id}`, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project', projectId, 'candidates'] });
      queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not update the moment', description: (error as Error).message }),
  });

  const createClip = useMutation({
    mutationFn: (candidateId: string) => api.post<{ id: string }>(`/candidates/${candidateId}/clip`),
    onSuccess: (clip) => {
      queryClient.invalidateQueries({ queryKey: ['project', projectId, 'candidates'] });
      queryClient.invalidateQueries({ queryKey: queryKeys.clips(projectId) });
      toast({ kind: 'success', title: 'Clip created', action: { label: 'Open editor', run: () => navigate(`/projects/${projectId}/editor/${clip.id}`) } });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not create the clip', description: (error as Error).message }),
  });

  const videoId = project.data?.latest_video?.id ?? null;
  const items = candidates.data?.items ?? [];
  const counts = useMemo(
    () => ({
      all: candidates.data?.total ?? 0,
      kept: items.filter((item) => item.status === 'kept').length,
      rejected: items.filter((item) => item.status === 'rejected').length,
    }),
    [candidates.data, items],
  );

  useEffect(() => {
    // Nothing to preload: clips are streamed by the player on demand.
  }, []);

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Review moments</h1>
          <p className="mt-1 text-sm text-slate-400">
            Each suggestion is scored from your transcript and audio. Keep what works, reject the rest.
          </p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn-secondary btn-sm" onClick={() => discover.mutate()} disabled={discover.isPending}>
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
            Re-run discovery
          </button>
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={(clips.data?.total ?? 0) === 0}
            onClick={() => navigate(`/projects/${projectId}/export`)}
          >
            Exports ({clips.data?.total ?? 0})
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {(['all', 'pending', 'kept', 'rejected'] as const).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setFilter(key)}
            className={cn(
              'rounded-pill px-3 py-1.5 text-xs font-semibold capitalize transition-colors',
              filter === key ? 'bg-ink-700 text-slate-100' : 'text-slate-400 hover:bg-ink-800 hover:text-slate-200',
            )}
          >
            {key}
            {key === 'kept' && counts.kept ? ` (${counts.kept})` : ''}
            {key === 'rejected' && counts.rejected ? ` (${counts.rejected})` : ''}
          </button>
        ))}
        {discover.isPending ? <ProgressBar className="ml-2 max-w-[160px]" value={15} /> : null}
      </div>

      {candidates.isPending ? (
        <div className="space-y-4">
          {[0, 1, 2].map((index) => (
            <Card key={index} className="h-56">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="mt-3 h-5 w-2/3" />
              <Skeleton className="mt-4 h-20 w-full" />
            </Card>
          ))}
        </div>
      ) : candidates.isError ? (
        <ErrorState
          message={candidates.error instanceof Error ? candidates.error.message : 'Could not load suggestions.'}
          onRetry={() => void candidates.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon={<Scissors className="h-8 w-8" />}
          title="No moments found yet"
          description="Clip discovery runs on a completed transcript. Start it from the project screen once transcription finishes."
          action={
            <button type="button" className="btn-primary" onClick={() => discover.mutate()} disabled={discover.isPending}>
              <Sparkles className="h-4 w-4" aria-hidden />
              Find clips now
            </button>
          }
        />
      ) : (
        <div className="space-y-4">
          {items.map((candidate) => (
            <CandidateCard
              key={candidate.id}
              candidate={candidate}
              videoId={videoId}
              busy={setStatus.isPending || createClip.isPending}
              onKeep={() => setStatus.mutate({ id: candidate.id, status: 'kept' })}
              onReject={() => setStatus.mutate({ id: candidate.id, status: 'rejected' })}
              onMakeClip={() => createClip.mutate(candidate.id)}
            />
          ))}
        </div>
      )}

      {(clips.data?.total ?? 0) > 0 ? (
        <section>
          <SectionHeader title="Clips" description={`${clips.data?.total} created`} />
          <div className="space-y-2">
            {clips.data?.items.map((clip) => (
              <div
                key={clip.id}
                className="flex items-center justify-between gap-4 rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-200">{clip.title}</p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {formatDuration(clip.duration)} · {clip.status}
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    onClick={() => navigate(`/projects/${projectId}/editor/${clip.id}`)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn-ghost btn-sm text-red-300"
                    onClick={async () => {
                      try {
                        await api.delete(`/clips/${clip.id}`);
                        queryClient.invalidateQueries({ queryKey: queryKeys.clips(projectId) });
                        toast({ kind: 'success', title: 'Clip deleted' });
                      } catch (error) {
                        toast({ kind: 'error', title: 'Could not delete the clip', description: (error as Error).message });
                      }
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
