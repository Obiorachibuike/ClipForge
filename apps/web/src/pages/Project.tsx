import { useCallback, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, FileVideo, Play, RefreshCw, Scissors, Upload, X } from 'lucide-react';
import { ApiError, api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useProject } from '@/hooks/useProjects';
import { useLive } from '@/lib/live';
import { cn, formatBytes, formatDuration, jobLabel } from '@/lib/format';
import { Card, ErrorState, ProgressBar, SectionHeader, Skeleton, StatusDot } from '@/components/ui';
import { toast } from '@/stores/ui';
import { useUpload } from '@/hooks/useUpload';
import type { Job, ProjectSummary, Video } from '@/types/api';

function VideoPick({ projectId }: { projectId: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const upload = useUpload(projectId);
  const [dragging, setDragging] = useState(false);

  const pick = useCallback(
    (file: File | undefined) => {
      if (!file) return;
      void upload.start(file);
    },
    [upload],
  );

  return (
    <div
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        pick(event.dataTransfer.files?.[0]);
      }}
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-card border-2 border-dashed px-6 py-12 text-center transition-colors',
        dragging ? 'border-accent-500 bg-accent-500/5' : 'border-ink-600 bg-ink-900/40',
      )}
    >
      <Upload className="h-7 w-7 text-slate-500" aria-hidden />
      <div>
        <p className="text-sm font-medium text-slate-200">Drop a video here, or choose a file</p>
        <p className="mt-1 text-xs text-slate-500">
          MP4, MOV, MKV, WebM, M4V or an audio file. Uploads are chunked and resumable.
        </p>
      </div>
      <button type="button" className="btn-primary btn-sm" onClick={() => inputRef.current?.click()}>
        Choose file
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="video/*,audio/*"
        className="hidden"
        onChange={(event) => pick(event.target.files?.[0] ?? undefined)}
      />

      {upload.status !== 'idle' ? (
        <div className="mt-3 w-full max-w-md">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="truncate">{upload.filename}</span>
            <span className="tabular-nums">{Math.round(upload.progress)}%</span>
          </div>
          <ProgressBar
            className="mt-2"
            value={upload.progress}
            tone={upload.status === 'failed' ? 'danger' : upload.status === 'done' ? 'success' : 'accent'}
          />
          <div className="mt-2 flex items-center justify-between">
            <p className="text-xs text-slate-500">{upload.message}</p>
            {upload.status === 'uploading' ? (
              <button type="button" className="btn-ghost btn-sm" onClick={upload.cancel}>
                <X className="h-3.5 w-3.5" aria-hidden />
                Cancel
              </button>
            ) : null}
            {upload.status === 'failed' ? (
              <button type="button" className="btn-secondary btn-sm" onClick={() => void upload.resume()}>
                <RefreshCw className="h-3.5 w-3.5" aria-hidden />
                Resume
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function VideoRow({ video }: { video: Video }) {
  return (
    <div className="flex items-center gap-4 rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-ink-800 text-slate-400">
        <FileVideo className="h-5 w-5" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-200">{video.original_filename}</p>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
          <span className="inline-flex items-center gap-1.5">
            <StatusDot
              tone={
                video.status === 'ready'
                  ? 'success'
                  : video.status === 'failed'
                    ? 'danger'
                    : video.status === 'pending'
                      ? 'neutral'
                      : 'accent'
              }
            />
            {video.status}
          </span>
          {video.duration_seconds ? <span>{formatDuration(video.duration_seconds)}</span> : null}
          {video.width ? (
            <span className="font-mono">
              {video.width}×{video.height}
            </span>
          ) : null}
          {video.fps ? <span>{video.fps.toFixed(2)} fps</span> : null}
          <span>{formatBytes(video.size_bytes)}</span>
        </p>
        {video.error_message ? <p className="mt-1 text-xs text-red-300">{video.error_message}</p> : null}
      </div>
    </div>
  );
}

function PipelineActions({ project }: { project: ProjectSummary }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { connection } = useLive();
  const video = project.latest_video;

  const jobs = useQuery({
    queryKey: queryKeys.projectJobs(project.id),
    queryFn: () => api.get<Job[]>(`/projects/${project.id}/jobs`),
    refetchInterval: connection === 'open' ? false : 4000,
  });

  const active = (jobs.data ?? []).filter((job) => ['queued', 'running', 'retrying'].includes(job.status));

  const transcribe = useMutation({
    mutationFn: () => api.post<Job>(`/videos/${video?.id}/transcribe`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projectJobs(project.id) });
      toast({ kind: 'info', title: 'Transcription queued' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not start transcription', description: (error as Error).message }),
  });

  const discover = useMutation({
    mutationFn: () => api.post<Job>(`/projects/${project.id}/discover`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projectJobs(project.id) });
      toast({ kind: 'info', title: 'Clip discovery queued' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not start clip discovery', description: (error as Error).message }),
  });

  const transcript = useQuery({
    queryKey: queryKeys.projectTranscript(project.id),
    queryFn: () => api.get<{ status: string; word_count: number } | null>(`/projects/${project.id}/transcript`),
    enabled: Boolean(video),
  });

  const transcriptReady = transcript.data?.status === 'completed';

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-secondary btn-sm"
          disabled={!video || video.status !== 'ready' || transcribe.isPending}
          onClick={() => transcribe.mutate()}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
          {transcriptReady ? 'Re-transcribe' : 'Transcribe'}
        </button>
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={!transcriptReady || discover.isPending}
          onClick={() => discover.mutate()}
        >
          <Scissors className="h-3.5 w-3.5" aria-hidden />
          Find clips
        </button>
        <Link
          to={`/projects/${project.id}/analyze`}
          className={cn('btn-secondary btn-sm', !video && 'pointer-events-none opacity-50')}
        >
          Open analysis
        </Link>
        {(project.candidate_count ?? 0) > 0 ? (
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => navigate(`/projects/${project.id}/clips`)}
          >
            <Play className="h-3.5 w-3.5" aria-hidden />
            Review {project.candidate_count} moments
          </button>
        ) : null}
      </div>

      {active.length > 0 ? (
        <div className="space-y-2">
          {active.map((job) => (
            <div key={job.id} className="rounded-xl border border-accent-500/25 bg-accent-500/[0.06] px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium text-slate-200">{jobLabel(job.type)}</p>
                <span className="text-2xs tabular-nums text-slate-400">{Math.round(job.progress)}%</span>
              </div>
              <p className="mt-0.5 text-xs text-slate-400">{job.message}</p>
              <ProgressBar className="mt-2" value={job.progress} />
            </div>
          ))}
        </div>
      ) : null}

      {!video ? (
        <p className="text-xs text-slate-500">Upload a video to unlock transcription and clip discovery.</p>
      ) : null}
    </div>
  );
}

export default function ProjectPage() {
  const { projectId = '' } = useParams();
  const project = useProject(projectId);

  if (project.isPending) {
    return (
      <div className="mx-auto max-w-5xl space-y-6">
        <Skeleton className="h-8 w-1/3" />
        <Card className="h-48">
          <Skeleton className="h-full w-full" />
        </Card>
      </div>
    );
  }

  if (project.isError) {
    const error = project.error;
    return (
      <div className="mx-auto max-w-5xl">
        <ErrorState
          message={
            error instanceof ApiError && error.status === 404
              ? 'That project does not exist, or it belongs to another account.'
              : error instanceof Error
                ? error.message
                : 'Could not load the project.'
          }
          onRetry={() => void project.refetch()}
        />
      </div>
    );
  }

  const data = project.data!;
  const videoCount = data.video_count ?? 0;

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-2xl font-semibold tracking-tight text-white">{data.name}</h1>
            <span className="badge-neutral">{data.status}</span>
          </div>
          <p className="mt-1 text-sm text-slate-400">
            {data.description || `Target format ${data.target_aspect_ratio} · ${data.privacy_mode} mode`}
          </p>
        </div>
      </div>

      <PipelineActions project={data} />

      <section>
        <SectionHeader
          title="Source media"
          description={videoCount > 0 ? `${videoCount} file${videoCount === 1 ? '' : 's'}` : undefined}
        />
        {data.latest_video ? (
          <div className="space-y-3">
            <VideoRow video={data.latest_video} />
            {data.latest_video.status === 'ready' ? (
              <div className="overflow-hidden rounded-card border border-ink-700/70 bg-black">
                <video
                  className="max-h-[420px] w-full"
                  controls
                  preload="metadata"
                  src={`/api/v1/videos/${data.latest_video.id}/stream`}
                />
              </div>
            ) : null}
          </div>
        ) : (
          <VideoPick projectId={projectId} />
        )}
      </section>

      {videoCount > 0 ? (
        <section>
          <SectionHeader title="Add another source" description="Each video is processed independently." />
          <VideoPick projectId={projectId} />
        </section>
      ) : null}

      {data.stage === 'failed' ? (
        <div className="flex items-start gap-3 rounded-card border border-amber-500/30 bg-amber-500/[0.07] px-4 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" aria-hidden />
          <p className="text-sm text-amber-200">
            The last job failed. Open the analysis screen for the error and retry from there.
          </p>
        </div>
      ) : null}
    </div>
  );
}
