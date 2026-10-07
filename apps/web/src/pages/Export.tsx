import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { Download, Film, Package, Trash2 } from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useLive } from '@/lib/live';
import { useQueryClient } from '@tanstack/react-query';
import { formatBytes, formatDateTime, formatDuration, titleCase } from '@/lib/format';
import { Card, EmptyState, ErrorState, ProgressBar, SectionHeader, Skeleton } from '@/components/ui';
import { toast } from '@/stores/ui';
import type { ExportItem, Page, Render } from '@/types/api';

export default function ExportPage() {
  const { projectId = '' } = useParams();
  const queryClient = useQueryClient();
  const { connection } = useLive();

  const exports = useQuery({
    queryKey: queryKeys.exports(projectId),
    queryFn: () => api.get<Page<ExportItem>>(`/projects/${projectId}/exports`, { limit: 50 }),
    refetchInterval: connection === 'open' ? false : 8000,
  });

  const renders = useQuery({
    queryKey: queryKeys.projectRenders(projectId),
    queryFn: () => api.get<Page<Render>>(`/projects/${projectId}/renders`, { limit: 50 }),
    refetchInterval: connection === 'open' ? false : 5000,
  });

  const activeRenders = (renders.data?.items ?? []).filter((render) =>
    ['queued', 'running'].includes(render.status),
  );

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Exports</h1>
        <p className="mt-1 text-sm text-slate-400">
          Rendered MP4s are stored once and downloadable per platform preset.
        </p>
      </div>

      {activeRenders.length > 0 ? (
        <section>
          <SectionHeader title="Rendering now" />
          <div className="space-y-2">
            {activeRenders.map((render) => (
              <Card key={render.id}>
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-medium text-slate-200">
                    {titleCase(render.preset)} · {render.width}×{render.height}
                  </p>
                  <span className="text-2xs tabular-nums text-slate-400">{Math.round(render.progress)}%</span>
                </div>
                <p className="mt-1 text-xs text-slate-500">{render.message || render.stage}</p>
                <ProgressBar className="mt-2" value={render.progress} />
              </Card>
            ))}
          </div>
        </section>
      ) : null}

      <section>
        <SectionHeader
          title="Files"
          description={exports.data ? `${exports.data.total} export${exports.data.total === 1 ? '' : 's'}` : undefined}
        />

        {exports.isPending ? (
          <div className="space-y-3">
            {[0, 1, 2].map((index) => (
              <Card key={index} className="h-24">
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="mt-3 h-3 w-1/4" />
              </Card>
            ))}
          </div>
        ) : exports.isError ? (
          <ErrorState
            message={exports.error instanceof Error ? exports.error.message : 'Could not load exports.'}
            onRetry={() => void exports.refetch()}
          />
        ) : (exports.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            icon={<Package className="h-8 w-8" />}
            title="No exports yet"
            description="Open a clip, choose a platform preset and render it. The finished file appears here with a download link."
            action={
              <Link to={`/projects/${projectId}/clips`} className="btn-primary">
                <Film className="h-4 w-4" aria-hidden />
                Review clips
              </Link>
            }
          />
        ) : (
          <div className="space-y-3">
            {exports.data?.items.map((item) => (
              <Card key={item.id} className="flex flex-wrap items-center justify-between gap-4">
                <div className="flex min-w-0 items-center gap-4">
                  {item.thumbnail_key ? (
                    <img
                      src={`/api/v1/exports/${item.id}/thumbnail`}
                      alt=""
                      className="h-14 w-14 shrink-0 rounded-lg border border-ink-700 object-cover"
                      loading="lazy"
                    />
                  ) : (
                    <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-lg bg-ink-800 text-slate-500">
                      <Film className="h-6 w-6" aria-hidden />
                    </span>
                  )}
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-200">
                      {item.clip_title || item.filename}
                    </p>
                    <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                      <span className="badge-neutral">{titleCase(item.platform)}</span>
                      <span className="font-mono">
                        {item.width}×{item.height}
                      </span>
                      <span>{formatDuration(item.duration_seconds)}</span>
                      <span>{formatBytes(item.size_bytes)}</span>
                      <span>{formatDateTime(item.created_at)}</span>
                      {item.download_count > 0 ? <span>{item.download_count} downloads</span> : null}
                    </p>
                  </div>
                </div>
                <div className="flex shrink-0 gap-2">
                  <a
                    className="btn-secondary btn-sm"
                    href={`/api/v1/exports/${item.id}/download`}
                    download={item.filename}
                  >
                    <Download className="h-3.5 w-3.5" aria-hidden />
                    Download
                  </a>
                  <button
                    type="button"
                    className="btn-ghost btn-sm text-red-300"
                    onClick={async () => {
                      try {
                        await api.delete(`/exports/${item.id}`);
                        queryClient.invalidateQueries({ queryKey: queryKeys.exports(projectId) });
                        toast({ kind: 'success', title: 'Export deleted' });
                      } catch (error) {
                        toast({
                          kind: 'error',
                          title: 'Could not delete the export',
                          description: (error as Error).message,
                        });
                      }
                    }}
                    aria-label="Delete export"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
