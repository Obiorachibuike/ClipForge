import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, FileText, Info, Loader2 } from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { Card, SectionHeader } from '@/components/ui';
import { toast } from '@/stores/ui';
import type { NarrationScript, Video } from '@/types/api';

/**
 * Narration script panel.
 *
 * When a script is stored, transcription aligns it to the audio with DTW and
 * produces real word-level timings — no ASR weights, no network, no guessing.
 * This is the offline path to captions and clip discovery, so it is a first-class
 * control rather than a hidden API feature.
 *
 * The copy is explicit that alignment measures the audio against the script: it
 * is not a transcript of speech that was never written down.
 */
export default function ScriptPanel({ video, projectId }: { video: Video; projectId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);

  const script = useQuery({
    queryKey: queryKeys.videoScript(video.id),
    queryFn: () => api.get<NarrationScript>(`/videos/${video.id}/narration-script`),
  });

  // Load the stored script once, without clobbering edits in progress.
  useEffect(() => {
    if (script.data && !dirty) {
      setDraft(script.data.narration_script);
    }
  }, [script.data, dirty]);

  const save = useMutation({
    mutationFn: (narration_script: string) =>
      api.patch<Video>(`/videos/${video.id}`, { narration_script }),
    onSuccess: (updated) => {
      setDirty(false);
      queryClient.setQueryData<NarrationScript>(queryKeys.videoScript(video.id), () => ({
        video_id: video.id,
        narration_script: draft,
        has_narration_script: Boolean(draft.trim()),
        character_count: draft.length,
        word_count: draft.split(/\s+/).filter(Boolean).length,
      }));
      queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.projectTranscript(projectId) });
      toast({
        kind: 'success',
        title: updated.has_narration_script ? 'Script saved' : 'Script cleared',
        description: updated.has_narration_script
          ? 'The next transcription will align it to the audio for exact word timings.'
          : 'Transcription will use a speech-to-text provider instead.',
      });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not save the script', description: (error as Error).message }),
  });

  const stored = script.data?.has_narration_script ?? video.has_narration_script;
  const wordCount = draft.split(/\s+/).filter(Boolean).length;

  return (
    <Card>
      <SectionHeader
        title="Narration script"
        description="Optional. Paste the words spoken in this video and transcription aligns them to the audio for exact, word-level timings — no speech model required."
        action={
          stored ? (
            <span className="badge-success">
              <CheckCircle2 className="h-3 w-3" aria-hidden />
              saved
            </span>
          ) : null
        }
      />

      {script.isPending ? (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          Loading script…
        </div>
      ) : (
        <>
          <label className="label" htmlFor="narration-script">
            Script text
          </label>
          <textarea
            id="narration-script"
            className="input min-h-40 font-mono text-xs leading-relaxed"
            placeholder={'Paste the spoken words here, in order.\n\nMost people get this completely wrong. You do not need a bigger audience…'}
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setDirty(true);
            }}
            spellCheck={false}
          />

          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p className="flex items-center gap-2 text-xs text-slate-500">
              <FileText className="h-3.5 w-3.5" aria-hidden />
              {wordCount.toLocaleString()} word{wordCount === 1 ? '' : 's'}
              {dirty ? ' · unsaved changes' : stored ? ' · saved' : ''}
            </p>
            <div className="flex gap-2">
              {stored ? (
                <button
                  type="button"
                  className="btn-ghost btn-sm text-red-300"
                  disabled={save.isPending}
                  onClick={() => {
                    setDraft('');
                    setDirty(true);
                    save.mutate('');
                  }}
                >
                  Remove script
                </button>
              ) : null}
              <button
                type="button"
                className="btn-primary btn-sm"
                disabled={save.isPending || !dirty || !draft.trim()}
                onClick={() => save.mutate(draft.trim())}
              >
                {save.isPending ? 'Saving…' : 'Save script'}
              </button>
            </div>
          </div>

          <p className="mt-4 flex items-start gap-2 rounded-xl border border-ink-700/70 bg-ink-900/40 px-4 py-3 text-xs leading-relaxed text-slate-400">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent-300" aria-hidden />
            <span>
              Alignment measures your audio against the script you paste. It produces real timings for the words in
              the script — not a transcript of anything you left out. If you change the script later, the existing
              transcript is marked superseded so stale timings are never shown as current.
            </span>
          </p>
        </>
      )}
    </Card>
  );
}
