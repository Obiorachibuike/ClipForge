import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle, Captions, FileSearch, Users } from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { cn, formatTime } from '@/lib/format';
import { Card, EmptyState, ProgressBar, SectionHeader, Skeleton, Stat } from '@/components/ui';
import type { Capabilities, Job, Transcript } from '@/types/api';

type Tab = 'transcript' | 'insights' | 'jobs';

function WordList({ transcript }: { transcript: Transcript }) {
  const [query, setQuery] = useState('');
  const words = transcript.words ?? [];
  const filtered = useMemo(() => {
    if (!query.trim()) return null;
    const needle = query.trim().toLowerCase();
    return transcript.segments?.filter((segment) => segment.text.toLowerCase().includes(needle)) ?? [];
  }, [query, transcript.segments]);

  if (words.length === 0 && !transcript.segments?.length) {
    return (
      <EmptyState
        icon={<Captions className="h-7 w-7" />}
        title="No transcript yet"
        description="Run transcription from the project screen. Word-level timings are required for captions and clip discovery."
      />
    );
  }

  if (filtered) {
    return (
      <div className="space-y-2">
        <input
          className="input"
          placeholder="Search the transcript…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {filtered.length === 0 ? (
          <p className="px-1 py-6 text-center text-sm text-slate-500">
            No matching segment for “{query}”.
          </p>
        ) : (
          filtered.map((segment) => (
            <div key={segment.id} className="rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3">
              <div className="flex items-center gap-3 text-2xs text-slate-500">
                <span className="font-mono tabular-nums">{formatTime(segment.start_time)}</span>
                {segment.speaker ? <span className="badge-neutral">{segment.speaker}</span> : null}
                <span className="tabular-nums">{Math.round(segment.avg_confidence * 100)}% confidence</span>
              </div>
              <p className="mt-1.5 text-sm leading-relaxed text-slate-200">{segment.text}</p>
            </div>
          ))
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <input
        className="input"
        placeholder="Search the transcript…"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      <div className="max-h-[560px] overflow-y-auto rounded-xl border border-ink-700/70 bg-ink-900/40 p-4">
        <p className="text-sm leading-[1.9] text-slate-300">
          {(transcript.segments ?? []).map((segment) => (
            <span key={segment.id} className="inline">
              <span className="mr-1.5 font-mono text-2xs text-slate-600">{formatTime(segment.start_time)}</span>
              {segment.text}{' '}
            </span>
          ))}
        </p>
        {!transcript.segments?.length && words.length > 0 ? (
          <p className="text-sm leading-[1.9] text-slate-300">
            {words.map((word) => `${word.word} `).join('')}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function Insights({ transcript, projectId }: { transcript: Transcript | null; projectId: string }) {
  const framing = useQuery({
    queryKey: queryKeys.projectFraming(projectId),
    queryFn: () => api.get<Record<string, unknown>>(`/projects/${projectId}/framing`),
  });

  const meta = (transcript?.meta ?? {}) as Record<string, unknown>;
  const providerMeta = (meta.provider_meta ?? {}) as Record<string, unknown>;
  const alignment = (providerMeta.script_alignment ?? null) as Record<string, unknown> | null;
  const diarization = (meta.diarization ?? null) as Record<string, unknown> | null;
  const diagnostics = (providerMeta.alignment_diagnostics ?? null) as Record<string, unknown> | null;

  return (
    <div className="space-y-4">
      {alignment?.applied ? (
        <Card>
          <h3 className="text-sm font-semibold text-slate-100">Script alignment</h3>
          <p className="mt-1 text-sm text-slate-400">
            Word timings were produced by aligning the supplied script against the real audio.
          </p>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <Stat label="Alignment quality" value={`${Math.round((Number(alignment.quality_ratio) || 0) * 100)}%`} />
            <Stat label="Words aligned" value={String(alignment.words ?? 0)} />
            <Stat
              label="Path cost"
              value={diagnostics?.average_local_cost ? Number(diagnostics.average_local_cost).toFixed(2) : '—'}
            />
          </div>
          {alignment.warning ? (
            <p className="mt-3 text-xs text-amber-300">{String(alignment.warning)}</p>
          ) : null}
        </Card>
      ) : null}

      {diarization ? (
        <Card>
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <Users className="h-4 w-4 text-slate-400" aria-hidden />
            Speaker analysis
          </h3>
          <p className="mt-1 text-sm text-slate-400">
            {diarization.note
              ? String(diarization.note)
              : `${diarization.speakers ?? 1} speaker(s) separated from acoustic features.`}
          </p>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <Stat label="Speakers" value={String(diarization.speakers ?? 1)} />
            <Stat label="Voiced frames" value={String(diarization.voiced_frames ?? 0)} />
            <Stat
              label="Separation"
              value={diarization.separation ? Number(diarization.separation).toFixed(2) : '—'}
            />
          </div>
        </Card>
      ) : null}

      <Card>
        <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <Activity className="h-4 w-4 text-slate-400" aria-hidden />
          Framing analysis
        </h3>
        {framing.isPending ? (
          <Skeleton className="mt-3 h-16 w-full" />
        ) : framing.data && Object.keys(framing.data).length > 0 ? (
          <>
            <p className="mt-1 text-sm text-slate-400">
              Strategy <span className="text-slate-200">{String(framing.data.framing_strategy ?? 'n/a')}</span> using{' '}
              <span className="text-slate-200">{String(framing.data.detector ?? 'n/a')}</span>
            </p>
            <div className="mt-3 grid gap-3 sm:grid-cols-4">
              <Stat label="Faces found" value={String(framing.data.faces_detected ?? 0)} />
              <Stat label="Tracks" value={String(framing.data.tracks ?? 0)} />
              <Stat label="Frames analysed" value={String(framing.data.frames_analyzed ?? 0)} />
              <Stat
                label="Confidence"
                value={`${Math.round((Number(framing.data.tracking_confidence) || 0) * 100)}%`}
              />
            </div>
            <p className="mt-3 text-2xs text-slate-500">
              Crop keyframes are expressed in source pixels
              {framing.data.source_width
                ? ` (${framing.data.source_width}×${framing.data.source_height})`
                : ''}
              , which is what the renderer crops in.
            </p>
          </>
        ) : (
          <p className="mt-1 text-sm text-slate-400">
            Framing analysis has not run for this video yet. It runs automatically with clip discovery, or you can
            start it from the project screen.
          </p>
        )}
      </Card>
    </div>
  );
}

function JobsTab({ projectId }: { projectId: string }) {
  const jobs = useQuery({
    queryKey: queryKeys.projectJobs(projectId),
    queryFn: () => api.get<Job[]>(`/projects/${projectId}/jobs`),
  });

  if (jobs.isPending) return <Skeleton className="h-40 w-full" />;
  const items = jobs.data ?? [];
  if (items.length === 0) {
    return <EmptyState title="No jobs yet" description="Processing jobs appear here as they are queued." />;
  }

  return (
    <div className="space-y-2">
      {items.map((job) => (
        <div key={job.id} className="rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-medium text-slate-200">{job.type}</p>
            <span
              className={cn(
                'badge',
                job.status === 'succeeded'
                  ? 'badge-success'
                  : job.status === 'failed'
                    ? 'badge-danger'
                    : job.status === 'running'
                      ? 'badge-accent'
                      : 'badge-neutral',
              )}
            >
              {job.status}
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            {job.message} · {job.duration_seconds ? `${job.duration_seconds.toFixed(1)}s` : 'queued'}
          </p>
          {job.status === 'running' ? <ProgressBar className="mt-2" value={job.progress} /> : null}
          {job.status === 'failed' ? (
            <p className="mt-2 flex items-start gap-2 text-xs text-red-300">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              {job.error_message || job.error_code}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export default function AnalyzePage() {
  const { projectId = '' } = useParams();
  const [tab, setTab] = useState<Tab>('transcript');

  const transcript = useQuery({
    queryKey: queryKeys.projectTranscript(projectId),
    queryFn: () => api.get<Transcript | null>(`/projects/${projectId}/transcript`),
  });

  const capabilities = useQuery({
    queryKey: queryKeys.capabilities,
    queryFn: () => api.get<Capabilities>('/capabilities'),
    staleTime: 5 * 60_000,
  });

  const transcriptData = transcript.data && 'id' in (transcript.data as object) ? (transcript.data as Transcript) : null;
  const detail = useQuery({
    queryKey: queryKeys.transcript(transcriptData?.id ?? ''),
    queryFn: () => api.get<Transcript>(`/transcripts/${transcriptData!.id}?include_words=true`),
    enabled: Boolean(transcriptData?.id),
  });

  const tabs: Array<{ key: Tab; label: string; icon: typeof Captions }> = [
    { key: 'transcript', label: 'Transcript', icon: Captions },
    { key: 'insights', label: 'Analysis', icon: FileSearch },
    { key: 'jobs', label: 'Jobs', icon: Activity },
  ];

  const caps = capabilities.data;

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Analysis</h1>
          <p className="mt-1 text-sm text-slate-400">
            What ClipForge measured from your media: words, timings, speakers and framing.
          </p>
        </div>
        <Link to={`/projects/${projectId}/clips`} className="btn-secondary btn-sm">
          Go to clips
        </Link>
      </div>

      {caps && !caps.transcription.script_alignment?.available && !caps.transcription.local.available && !caps.transcription.api.available ? (
        <div className="flex items-start gap-3 rounded-card border border-amber-500/30 bg-amber-500/[0.07] px-4 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" aria-hidden />
          <div className="text-sm text-amber-100">
            <p className="font-medium">No transcription provider is available on this deployment.</p>
            <p className="mt-1 text-amber-200/80">
              Install faster-whisper with a cached model, set an OpenAI-compatible key, or paste the spoken script so
              ClipForge can align it to your audio.
            </p>
          </div>
        </div>
      ) : null}

      <div className="flex gap-1 rounded-xl border border-ink-700/70 bg-ink-900/50 p-1">
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={cn(
              'flex flex-1 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
              tab === item.key ? 'bg-ink-700 text-slate-100' : 'text-slate-400 hover:text-slate-200',
            )}
          >
            <item.icon className="h-4 w-4" aria-hidden />
            {item.label}
          </button>
        ))}
      </div>

      {transcriptData && (detail.data ?? transcriptData).word_count ? (
        <div className="grid gap-3 sm:grid-cols-4">
          <Stat label="Words" value={(detail.data ?? transcriptData).word_count} />
          <Stat label="Segments" value={(detail.data ?? transcriptData).segment_count} />
          <Stat label="Speakers" value={(detail.data ?? transcriptData).speaker_count} />
          <Stat
            label="Provider"
            value={<span className="text-base">{detail.data?.provider || transcriptData.provider}</span>}
            hint={`model ${detail.data?.model || transcriptData.model || 'n/a'}`}
          />
        </div>
      ) : null}

      <SectionHeader title={tab === 'transcript' ? 'Words and timing' : tab === 'insights' ? 'Measurements' : 'Job history'} />

      {tab === 'transcript' ? (
        transcript.isPending ? (
          <Skeleton className="h-64 w-full" />
        ) : detail.isPending && transcriptData?.id ? (
          <Skeleton className="h-64 w-full" />
        ) : (
          <WordList transcript={detail.data ?? transcriptData ?? ({ words: [], segments: [] } as unknown as Transcript)} />
        )
      ) : tab === 'insights' ? (
        <Insights transcript={detail.data ?? transcriptData} projectId={projectId} />
      ) : (
        <JobsTab projectId={projectId} />
      )}
    </div>
  );
}
