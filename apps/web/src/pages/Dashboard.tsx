import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Clock, Film, Layers, Plus, Scissors, Timer, Upload, Zap } from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useProjects, useCreateProject } from '@/hooks/useProjects';
import { useLive } from '@/lib/live';
import { useAuthStore } from '@/stores/auth';
import { cn, formatBytes, formatDuration, formatRelative, jobLabel, titleCase } from '@/lib/format';
import { Card, EmptyState, ErrorState, ProgressBar, SectionHeader, Skeleton, Stat } from '@/components/ui';
import type { Job, ProjectSummary, UsageSummary } from '@/types/api';

function JobRow({ job }: { job: Job }) {
  const tone = job.status === 'failed' ? 'danger' : job.status === 'succeeded' ? 'success' : 'accent';
  return (
    <div className="flex items-center gap-3 rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3">
      <Zap
        className={cn(
          'h-4 w-4 shrink-0',
          tone === 'danger' ? 'text-red-400' : tone === 'success' ? 'text-emerald-400' : 'text-accent-400',
        )}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="truncate text-sm font-medium text-slate-200">{jobLabel(job.type)}</p>
          <span className="shrink-0 text-2xs tabular-nums text-slate-500">{Math.round(job.progress)}%</span>
        </div>
        <p className="mt-0.5 truncate text-xs text-slate-500">
          {job.status === 'failed' ? job.error_message || 'Failed' : job.message || titleCase(job.status)}
        </p>
        {job.status === 'running' || job.status === 'queued' ? (
          <ProgressBar className="mt-2" value={job.progress} tone={tone} />
        ) : null}
      </div>
    </div>
  );
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  const busy = project.active_jobs?.length > 0;
  return (
    <Link to={`/projects/${project.id}`} className="card card-hover group block p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-white group-hover:text-accent-200">{project.name}</h3>
          <p className="mt-0.5 line-clamp-1 text-xs text-slate-500">
            {project.description || formatRelative(project.updated_at)}
          </p>
        </div>
        <span
          className={cn(
            'badge shrink-0',
            project.status === 'ready'
              ? 'badge-success'
              : project.status === 'failed'
                ? 'badge-danger'
                : busy
                  ? 'badge-warning'
                  : 'badge-neutral',
          )}
        >
          {busy ? project.stage || 'working' : project.status}
        </span>
      </div>

      {busy ? <ProgressBar className="mt-4" value={project.progress || 0} /> : null}

      <div className="mt-4 flex flex-wrap items-center gap-4 text-xs text-slate-500">
        <span className="inline-flex items-center gap-1.5">
          <Film className="h-3.5 w-3.5" aria-hidden />
          {project.video_count} video{project.video_count === 1 ? '' : 's'}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Layers className="h-3.5 w-3.5" aria-hidden />
          {project.candidate_count} moments
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Scissors className="h-3.5 w-3.5" aria-hidden />
          {project.clip_count} clip{project.clip_count === 1 ? '' : 's'}
        </span>
        {project.total_duration_seconds ? (
          <span className="inline-flex items-center gap-1.5">
            <Timer className="h-3.5 w-3.5" aria-hidden />
            {formatDuration(project.total_duration_seconds)} processed
          </span>
        ) : null}
      </div>
    </Link>
  );
}

function NewProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [name, setName] = useState('');
  const [aspect, setAspect] = useState('9:16');
  const [privacy, setPrivacy] = useState('cloud');
  const create = useCreateProject();

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div className="card relative w-full max-w-md p-6">
        <h2 className="text-lg font-semibold text-white">New project</h2>
        <p className="mt-1 text-sm text-slate-400">
          Upload a long video and ClipForge will transcribe, analyse and suggest clips.
        </p>
        <form
          className="mt-5 space-y-4"
          onSubmit={async (event) => {
            event.preventDefault();
            await create.mutateAsync({
              name: name.trim() || 'Untitled project',
              target_aspect_ratio: aspect,
              privacy_mode: privacy,
            });
            setName('');
            onClose();
          }}
        >
          <div>
            <label className="label" htmlFor="project-name">
              Project name
            </label>
            <input
              id="project-name"
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Podcast episode 42"
              autoFocus
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label" htmlFor="project-aspect">
                Target format
              </label>
              <select
                id="project-aspect"
                className="input"
                value={aspect}
                onChange={(event) => setAspect(event.target.value)}
              >
                <option value="9:16">Vertical 9:16</option>
                <option value="1:1">Square 1:1</option>
                <option value="16:9">Landscape 16:9</option>
              </select>
            </div>
            <div>
              <label className="label" htmlFor="project-privacy">
                Privacy mode
              </label>
              <select
                id="project-privacy"
                className="input"
                value={privacy}
                onChange={(event) => setPrivacy(event.target.value)}
              >
                <option value="cloud">ClipForge Cloud</option>
                <option value="private_ai">Private AI providers</option>
              </select>
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" className="btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={create.isPending}>
              {create.isPending ? 'Creating…' : 'Create project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const projects = useProjects({ limit: 24 });
  const user = useAuthStore((state) => state.user);
  const { connection } = useLive();

  const activeJobs = useQuery({
    queryKey: queryKeys.jobsActive,
    queryFn: () => api.get<Job[]>('/jobs/active'),
    // When the socket is live, events drive this list; polling only covers gaps.
    refetchInterval: connection === 'open' ? false : 5000,
  });

  const usage = useQuery({
    queryKey: queryKeys.usage,
    queryFn: () => api.get<UsageSummary>('/usage'),
  });

  const jobs = activeJobs.data ?? [];
  const metrics = usage.data?.metrics ?? {};
  const minutes = metrics.minutes_processed;
  const renders = metrics.renders;

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-white">
            {user?.name ? `Welcome back, ${user.name.split(' ')[0]}` : 'Dashboard'}
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            Upload a long video, and ClipForge finds the moments worth posting.
          </p>
        </div>
        <button type="button" className="btn-primary" onClick={() => setDialogOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden />
          New project
        </button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Minutes processed"
          value={minutes ? Math.round(minutes.used) : 0}
          hint={minutes?.limit ? `of ${Math.round(minutes.limit)} this month` : 'this month'}
        />
        <Stat
          label="Clips generated"
          value={metrics.clips_generated ? Math.round(metrics.clips_generated.used) : 0}
          hint={metrics.clips_generated?.limit ? `limit ${Math.round(metrics.clips_generated.limit)}` : undefined}
        />
        <Stat
          label="Renders"
          value={renders ? Math.round(renders.used) : 0}
          hint={renders?.limit ? `limit ${Math.round(renders.limit)}` : undefined}
        />
        <Stat
          label="Storage"
          value={formatBytes(metrics.storage_used?.used ?? 0)}
          hint={metrics.storage_used?.limit ? `of ${formatBytes(metrics.storage_used.limit)}` : undefined}
        />
      </div>

      {jobs.length > 0 ? (
        <section>
          <SectionHeader
            title="Working now"
            description="Live progress from the worker queue."
            action={
              <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
                <Clock className="h-3.5 w-3.5" aria-hidden />
                {connection === 'open' ? 'streaming' : 'refreshing over the API'}
              </span>
            }
          />
          <div className="space-y-2">
            {jobs.slice(0, 4).map((job) => (
              <JobRow key={job.id} job={job} />
            ))}
          </div>
        </section>
      ) : null}

      <section>
        <SectionHeader title="Projects" description={`${projects.data?.total ?? 0} total`} />

        {projects.isPending ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {[0, 1, 2].map((index) => (
              <Card key={index} className="h-40">
                <Skeleton className="h-5 w-2/3" />
                <Skeleton className="mt-3 h-3 w-1/3" />
                <Skeleton className="mt-6 h-3 w-full" />
              </Card>
            ))}
          </div>
        ) : projects.isError ? (
          <ErrorState
            message={projects.error instanceof Error ? projects.error.message : 'Could not load projects.'}
            onRetry={() => void projects.refetch()}
          />
        ) : projects.data && projects.data.items.length > 0 ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {projects.data.items.map((project) => (
              <ProjectCard key={project.id} project={project} />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={<Upload className="h-8 w-8" />}
            title="No projects yet"
            description="Create a project and upload a long video. Everything after that — transcription, clip discovery, framing and rendering — happens automatically."
            action={
              <button type="button" className="btn-primary" onClick={() => setDialogOpen(true)}>
                <Plus className="h-4 w-4" aria-hidden />
                Create your first project
              </button>
            }
          />
        )}
      </section>

      <NewProjectDialog open={dialogOpen} onClose={() => setDialogOpen(false)} />
    </div>
  );
}
