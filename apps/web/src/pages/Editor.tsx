import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Captions,
  Check,
  Copy,
  Crop,
  Download,
  Film,
  ListVideo,
  Maximize,
  Pause,
  Play,
  Rewind,
  Save,
  Sparkles,
  Trash2,
  Volume2,
  VolumeX,
  X,
} from 'lucide-react';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useLive } from '@/lib/live';
import { cn, clamp, formatBytes, formatTime } from '@/lib/format';
import { Card, ProgressBar, SectionHeader, Skeleton } from '@/components/ui';
import { toast } from '@/stores/ui';
import type {
  CaptionResponse,
  ClipDetail,
  HeadlineResponse,
  Render,
  RenderPreset,
  Transcript,
} from '@/types/api';

type Tab = 'suggestions' | 'transcript' | 'captions';

const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
const ASPECTS = ['9:16', '1:1', '16:9'] as const;

/* -------------------------------------------------------------------------- */
/*                                    Player                                  */
/* -------------------------------------------------------------------------- */

function Player({
  clip,
  videoId,
  currentTime,
  videoRef,
}: {
  clip: ClipDetail;
  videoId: string | null;
  currentTime: number;
  videoRef: React.RefObject<HTMLVideoElement>;
}) {
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [speed, setSpeed] = useState(1);
  const shellRef = useRef<HTMLDivElement>(null);

  const duration = clip.duration;
  const relative = clamp(currentTime - clip.start_time, 0, duration);
  const progress = duration ? (relative / duration) * 100 : 0;

  const toggle = useCallback(() => {
    const element = videoRef.current;
    if (!element) return;
    if (element.paused) {
      if (element.currentTime < clip.start_time || element.currentTime >= clip.end_time) {
        element.currentTime = clip.start_time;
      }
      void element.play();
    } else {
      element.pause();
    }
  }, [clip.start_time, clip.end_time, videoRef]);

  const seek = useCallback(
    (ratio: number) => {
      const element = videoRef.current;
      if (!element) return;
      element.currentTime = clip.start_time + clamp(ratio, 0, 1) * duration;
    },
    [clip.start_time, duration, videoRef],
  );

  // Keep playback inside the clip boundaries without jumping the file itself.
  useEffect(() => {
    const element = videoRef.current;
    if (!element) return;
    const guard = () => {
      if (element.currentTime >= clip.end_time) {
        element.pause();
        element.currentTime = clip.end_time;
      }
    };
    element.addEventListener('timeupdate', guard);
    return () => element.removeEventListener('timeupdate', guard);
  }, [clip.end_time, videoRef]);

  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = speed;
  }, [speed, videoRef]);

  useEffect(() => {
    if (videoRef.current) videoRef.current.muted = muted;
  }, [muted, videoRef]);

  const fullscreen = () => {
    const element = shellRef.current;
    if (!element) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void element.requestFullscreen?.();
  };

  return (
    <div ref={shellRef} className="overflow-hidden rounded-card border border-ink-700 bg-black">
      <div className="relative">
        <video
          ref={videoRef}
          className="mx-auto max-h-[62vh] w-full bg-black"
          preload="metadata"
          playsInline
          src={videoId ? `/api/v1/videos/${videoId}/stream` : undefined}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
        />
        {!videoId ? (
          <div className="absolute inset-0 flex items-center justify-center bg-ink-900/80">
            <p className="text-sm text-slate-400">The source video for this clip is unavailable.</p>
          </div>
        ) : null}
      </div>

      <div className="space-y-3 border-t border-ink-700 bg-ink-900/80 px-4 py-3">
        <div
          className="timeline-track group"
          role="slider"
          aria-label="Clip position"
          aria-valuemin={0}
          aria-valuemax={Math.round(duration)}
          aria-valuenow={Math.round(relative)}
          tabIndex={0}
          onClick={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            seek((event.clientX - rect.left) / rect.width);
          }}
          onKeyDown={(event) => {
            if (event.key === 'ArrowRight') seek(progress / 100 + 0.02);
            if (event.key === 'ArrowLeft') seek(progress / 100 - 0.02);
          }}
        >
          <div className="timeline-progress" style={{ width: `${progress}%` }} />
          <div
            className="absolute top-1/2 h-3.5 w-3.5 -translate-y-1/2 rounded-full bg-white shadow-glow transition-transform group-hover:scale-110"
            style={{ left: `calc(${progress}% - 7px)` }}
          />
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className="btn-icon" onClick={toggle} aria-label={playing ? 'Pause' : 'Play'}>
            {playing ? <Pause className="h-5 w-5" /> : <Play className="h-5 w-5" />}
          </button>
          <button
            type="button"
            className="btn-icon"
            onClick={() => seek(0)}
            aria-label="Back to clip start"
            title="Jump to clip start"
          >
            <Rewind className="h-4 w-4" />
          </button>
          <span className="font-mono text-xs tabular-nums text-slate-300">
            {formatTime(relative, { tenths: true })} / {formatTime(duration)}
          </span>
          <span className="hidden font-mono text-2xs text-slate-500 sm:inline">
            source {formatTime(clip.start_time)} → {formatTime(clip.end_time)}
          </span>

          <div className="flex-1" />

          <button type="button" className="btn-icon" onClick={() => setMuted((value) => !value)} aria-label="Mute">
            {muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
          </button>
          <select
            className="rounded-lg border border-ink-600 bg-ink-800 px-2 py-1 text-xs text-slate-200"
            value={speed}
            onChange={(event) => setSpeed(Number(event.target.value))}
            aria-label="Playback speed"
          >
            {SPEEDS.map((value) => (
              <option key={value} value={value}>
                {value}×
              </option>
            ))}
          </select>
          <button type="button" className="btn-icon" onClick={fullscreen} aria-label="Fullscreen">
            <Maximize className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*                              Transcript / captions                         */
/* -------------------------------------------------------------------------- */

function TranscriptTab({
  clip,
  transcript,
  videoRef,
  onSeekTime,
}: {
  clip: ClipDetail;
  transcript: Transcript | null;
  videoRef: React.RefObject<HTMLVideoElement>;
  onSeekTime: (time: number) => void;
}) {
  const words = (transcript?.words ?? []).filter(
    (word) => word.end_time > clip.start_time && word.start_time < clip.end_time,
  );

  if (!words.length) {
    return (
      <p className="rounded-xl border border-dashed border-ink-600 px-4 py-8 text-center text-sm text-slate-500">
        No transcript covers this clip yet.
      </p>
    );
  }

  return (
    <div className="max-h-80 overflow-y-auto rounded-xl border border-ink-700/70 bg-ink-900/40 p-4">
      <p className="text-sm leading-[2]">
        {words.map((word) => (
          <button
            key={word.id}
            type="button"
            className="rounded px-0.5 text-slate-300 transition-colors hover:bg-accent-500/20 hover:text-white"
            onClick={() => {
              const element = videoRef.current;
              if (element) element.currentTime = word.start_time;
              onSeekTime(word.start_time);
            }}
            title={`${formatTime(word.start_time, { tenths: true })} · ${Math.round(word.confidence * 100)}% confidence`}
          >
            {word.word}
          </button>
        ))}
      </p>
    </div>
  );
}

function CaptionsTab({
  clip,
  captions,
  presets,
  onSave,
  saving,
}: {
  clip: ClipDetail;
  captions: CaptionResponse | undefined;
  presets: Array<{ key: string; label: string }>;
  onSave: (payload: { style?: Record<string, unknown>; word_edits?: Record<string, string>; preset?: string }) => void;
  saving: boolean;
}) {
  const [style, setStyle] = useState<Record<string, unknown>>({});
  const [edits, setEdits] = useState<Record<string, string>>({});

  useEffect(() => {
    setStyle((captions?.style ?? {}) as Record<string, unknown>);
    setEdits({});
  }, [captions?.style, clip.id]);

  const dirty = Object.keys(edits).length > 0 || JSON.stringify(style) !== JSON.stringify(captions?.style ?? {});

  const set = (key: string, value: unknown) => setStyle((current) => ({ ...current, [key]: value }));

  return (
    <div className="space-y-5">
      <div>
        <p className="label">Preset</p>
        <div className="flex flex-wrap gap-2">
          {presets.map((preset) => (
            <button
              key={preset.key}
              type="button"
              className={cn(
                'rounded-pill px-3 py-1.5 text-xs font-semibold transition-colors',
                style.preset === preset.key
                  ? 'bg-accent-500 text-white'
                  : 'border border-ink-600 bg-ink-800 text-slate-300 hover:border-ink-500',
              )}
              onClick={() => set('preset', preset.key)}
            >
              {preset.label}
            </button>
          ))}
        </div>
      </div>

      {captions?.cues?.length ? (
        <div>
          <p className="label">Lines (edit text — timing is preserved)</p>
          <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
            {captions.cues.map((cue, cueIndex) => (
              <div key={`${cue.start}-${cueIndex}`} className="rounded-lg border border-ink-700/70 bg-ink-900/50 p-3">
                <p className="font-mono text-2xs text-slate-500">
                  {formatTime(cue.start, { tenths: true })} → {formatTime(cue.end, { tenths: true })}
                </p>
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {cue.words.map((word) => (
                    <input
                      key={word.index}
                      className="w-auto min-w-[3rem] rounded border border-transparent bg-transparent px-1 py-0.5 text-sm text-slate-200 hover:border-ink-600 focus:border-accent-500 focus:outline-none"
                      value={edits[String(word.index)] ?? word.word}
                      onChange={(event) =>
                        setEdits((current) => ({ ...current, [String(word.index)]: event.target.value }))
                      }
                      size={Math.max(4, (edits[String(word.index)] ?? word.word).length)}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <p className="label">Position</p>
          <select
            className="input"
            value={String(style.position ?? 'bottom')}
            onChange={(event) => set('position', event.target.value)}
          >
            <option value="top">Top</option>
            <option value="middle">Middle</option>
            <option value="bottom">Bottom</option>
          </select>
        </div>
        <div>
          <p className="label">Animation</p>
          <select
            className="input"
            value={String(style.animation ?? 'pop')}
            onChange={(event) => set('animation', event.target.value)}
          >
            <option value="none">None</option>
            <option value="pop">Pop</option>
            <option value="fade">Fade</option>
            <option value="slide">Slide</option>
            <option value="karaoke">Karaoke highlight</option>
          </select>
        </div>
        <div>
          <p className="label">Text colour</p>
          <div className="flex gap-2">
            <input
              type="color"
              className="h-10 w-12 cursor-pointer rounded-lg border border-ink-600 bg-ink-800"
              value={String(style.text_color ?? '#FFFFFF')}
              onChange={(event) => set('text_color', event.target.value)}
            />
            <input
              className="input"
              value={String(style.text_color ?? '#FFFFFF')}
              onChange={(event) => set('text_color', event.target.value)}
            />
          </div>
        </div>
        <div>
          <p className="label">Highlight colour</p>
          <div className="flex gap-2">
            <input
              type="color"
              className="h-10 w-12 cursor-pointer rounded-lg border border-ink-600 bg-ink-800"
              value={String(style.highlight_color ?? '#7C5CFF')}
              onChange={(event) => set('highlight_color', event.target.value)}
            />
            <input
              className="input"
              value={String(style.highlight_color ?? '#7C5CFF')}
              onChange={(event) => set('highlight_color', event.target.value)}
            />
          </div>
        </div>
        <div>
          <p className="label">Font size — {String(style.font_size ?? 60)}</p>
          <input
            type="range"
            min={24}
            max={140}
            value={Number(style.font_size ?? 60)}
            onChange={(event) => set('font_size', Number(event.target.value))}
          />
        </div>
        <div>
          <p className="label">Max words per line — {String(style.max_words_per_line ?? 4)}</p>
          <input
            type="range"
            min={1}
            max={10}
            value={Number(style.max_words_per_line ?? 4)}
            onChange={(event) => set('max_words_per_line', Number(event.target.value))}
          />
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              className="h-4 w-4 accent-accent-500"
              checked={Boolean(style.uppercase)}
              onChange={(event) => set('uppercase', event.target.checked)}
            />
            Uppercase
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              className="h-4 w-4 accent-accent-500"
              checked={Boolean(style.bold ?? true)}
              onChange={(event) => set('bold', event.target.checked)}
            />
            Bold
          </label>
        </div>
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={!dirty || saving}
          onClick={() => onSave({ style, word_edits: edits })}
        >
          <Save className="h-3.5 w-3.5" aria-hidden />
          {saving ? 'Saving…' : 'Save captions'}
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*                                  Editor page                               */
/* -------------------------------------------------------------------------- */

export default function EditorPage() {
  const { projectId = '', clipId = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { connection } = useLive();
  const videoRef = useRef<HTMLVideoElement>(null);

  const [tab, setTab] = useState<Tab>('suggestions');
  const [currentTime, setCurrentTime] = useState(0);
  const [trim, setTrim] = useState<{ start: number; end: number } | null>(null);
  const [headlineDraft, setHeadlineDraft] = useState('');

  const clip = useQuery({
    queryKey: queryKeys.clip(clipId),
    queryFn: () => api.get<ClipDetail>(`/clips/${clipId}`),
    enabled: Boolean(clipId),
  });

  const captions = useQuery({
    queryKey: queryKeys.captions(clipId),
    queryFn: () => api.get<CaptionResponse>(`/clips/${clipId}/captions`),
    enabled: Boolean(clipId),
  });

  const headlines = useQuery({
    queryKey: queryKeys.headlines(clipId),
    queryFn: () => api.get<HeadlineResponse>(`/clips/${clipId}/headlines`),
    enabled: Boolean(clipId),
  });

  const projectTranscript = useQuery({
    queryKey: queryKeys.projectTranscript(projectId),
    queryFn: () => api.get<Transcript | null>(`/projects/${projectId}/transcript`),
    enabled: Boolean(projectId),
  });

  const renders = useQuery({
    queryKey: queryKeys.renders(clipId),
    queryFn: () => api.get<Render[]>(`/clips/${clipId}/renders`),
    enabled: Boolean(clipId),
    refetchInterval: (query) => {
      const latest = query.state.data?.[0];
      if (connection === 'open') return false;
      return latest && ['queued', 'running'].includes(latest.status) ? 2500 : false;
    },
  });

  const presets = useQuery({
    queryKey: queryKeys.renderPresets,
    queryFn: () => api.get<RenderPreset[]>('/render-presets'),
    staleTime: Infinity,
  });

  const captionPresets = useQuery({
    queryKey: queryKeys.captionPresets,
    queryFn: () => api.get<{ presets: Array<{ key: string; label: string }> }>('/caption-presets'),
    staleTime: Infinity,
  });

  useEffect(() => {
    if (clip.data) {
      setHeadlineDraft(clip.data.headline);
      setTrim({ start: clip.data.start_time, end: clip.data.end_time });
    }
  }, [clip.data]);

  useEffect(() => {
    const element = videoRef.current;
    if (!element) return;
    const onTimeUpdate = () => setCurrentTime(element.currentTime);
    element.addEventListener('timeupdate', onTimeUpdate);
    return () => element.removeEventListener('timeupdate', onTimeUpdate);
  }, [clip.data?.id]);

  const saveClip = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.patch<ClipDetail>(`/clips/${clipId}`, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.clip(clipId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.clips(projectId) });
      toast({ kind: 'success', title: 'Clip saved' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not save the clip', description: (error as Error).message }),
  });

  const saveCaptions = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.patch<CaptionResponse>(`/clips/${clipId}/captions`, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.captions(clipId) });
      toast({ kind: 'success', title: 'Captions updated' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not update captions', description: (error as Error).message }),
  });

  const generateHeadlines = useMutation({
    mutationFn: () => api.post<HeadlineResponse>(`/clips/${clipId}/headlines`, { count: 5 }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.headlines(clipId) });
      if (data.message) toast({ kind: 'info', title: data.message });
    },
  });

  const selectHeadline = useMutation({
    mutationFn: (text: string) => api.post<HeadlineResponse>(`/clips/${clipId}/headlines/select`, { text }),
    onSuccess: (data) => {
      setHeadlineDraft(data.current);
      queryClient.invalidateQueries({ queryKey: queryKeys.headlines(clipId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.clip(clipId) });
    },
  });

  const duplicate = useMutation({
    mutationFn: () => api.post<{ id: string }>(`/clips/${clipId}/duplicate`),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.clips(projectId) });
      toast({ kind: 'success', title: 'Clip duplicated' });
      navigate(`/projects/${projectId}/editor/${created.id}`);
    },
  });

  const render = useMutation({
    mutationFn: (preset: string) => api.post<Render>(`/clips/${clipId}/render`, { preset }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.renders(clipId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.jobsActive });
      toast({ kind: 'info', title: 'Render queued' });
    },
    onError: (error: unknown) =>
      toast({ kind: 'error', title: 'Could not start the render', description: (error as Error).message }),
  });

  const latestRender = renders.data?.[0];
  const activeRender = latestRender && ['queued', 'running'].includes(latestRender.status) ? latestRender : null;

  const transcriptForWords = useMemo(() => {
    const data = projectTranscript.data;
    return data && 'id' in (data as object) ? (data as Transcript) : null;
  }, [projectTranscript.data]);

  const candidate = clip.data?.candidate ?? null;

  if (clip.isPending) {
    return (
      <div className="mx-auto max-w-6xl space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-[420px] w-full" />
      </div>
    );
  }

  if (clip.isError || !clip.data) {
    return (
      <div className="mx-auto max-w-3xl">
        <Card>
          <p className="text-sm text-red-300">
            {clip.error instanceof Error ? clip.error.message : 'This clip could not be loaded.'}
          </p>
          <button type="button" className="btn-secondary btn-sm mt-4" onClick={() => navigate(`/projects/${projectId}/clips`)}>
            Back to clips
          </button>
        </Card>
      </div>
    );
  }

  const data = clip.data;
  const videoId = (data.video?.id as string) || null;

  const tabs: Array<{ key: Tab; label: string; icon: typeof Captions }> = [
    { key: 'suggestions', label: 'AI suggestions', icon: Sparkles },
    { key: 'transcript', label: 'Transcript', icon: Film },
    { key: 'captions', label: 'Captions', icon: Captions },
  ];

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold tracking-tight text-white">{data.title}</h1>
          <p className="mt-1 text-sm text-slate-400">
            {formatTime(data.start_time)} → {formatTime(data.end_time)} · {data.aspect_ratio} · {data.status}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => saveClip.mutate({ status: 'ready' })}
            disabled={saveClip.isPending}
          >
            <Check className="h-3.5 w-3.5" aria-hidden />
            Keep
          </button>
          <button
            type="button"
            className="btn-ghost btn-sm"
            onClick={() => duplicate.mutate()}
            disabled={duplicate.isPending}
          >
            <Copy className="h-3.5 w-3.5" aria-hidden />
            Duplicate
          </button>
          <button
            type="button"
            className="btn-ghost btn-sm text-red-300"
            onClick={async () => {
              try {
                await api.delete(`/clips/${clipId}`);
                queryClient.invalidateQueries({ queryKey: queryKeys.clips(projectId) });
                toast({ kind: 'success', title: 'Clip deleted' });
                navigate(`/projects/${projectId}/clips`);
              } catch (error) {
                toast({ kind: 'error', title: 'Could not delete the clip', description: (error as Error).message });
              }
            }}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
            Reject
          </button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-5">
          <Player clip={data} videoId={videoId} currentTime={currentTime} videoRef={videoRef} />

          <Card>
            <SectionHeader title="Edit" description="Lightweight trims and framing — not a full editor." />
            <div className="space-y-5">
              {trim ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <p className="label">Start — {formatTime(trim.start, { tenths: true })}</p>
                    <input
                      type="range"
                      min={0}
                      max={data.end_time - 1}
                      step={0.1}
                      value={trim.start}
                      onChange={(event) => setTrim({ ...trim, start: Number(event.target.value) })}
                    />
                  </div>
                  <div>
                    <p className="label">End — {formatTime(trim.end, { tenths: true })}</p>
                    <input
                      type="range"
                      min={trim.start + 1}
                      max={data.end_time + 60}
                      step={0.1}
                      value={trim.end}
                      onChange={(event) => setTrim({ ...trim, end: Number(event.target.value) })}
                    />
                  </div>
                </div>
              ) : null}

              <div className="grid gap-4 sm:grid-cols-3">
                <div>
                  <p className="label">Aspect ratio</p>
                  <select
                    className="input"
                    value={data.aspect_ratio}
                    onChange={(event) => saveClip.mutate({ aspect_ratio: event.target.value })}
                  >
                    {ASPECTS.map((aspect) => (
                      <option key={aspect} value={aspect}>
                        {aspect}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <p className="label">Framing</p>
                  <select
                    className="input"
                    value={data.crop.mode ?? 'auto'}
                    onChange={(event) =>
                      saveClip.mutate({ crop: { ...data.crop, mode: event.target.value } })
                    }
                  >
                    <option value="auto">
                      Auto ({(data.crop.strategy || 'tracking').replace(/_/g, ' ')})
                    </option>
                    <option value="center">Centre</option>
                    <option value="manual">Manual focus</option>
                  </select>
                </div>
                <div>
                  <p className="label">Crop keyframes</p>
                  <p className="flex items-center gap-2 rounded-xl border border-ink-600 bg-ink-900/60 px-3 py-2.5 text-sm text-slate-300">
                    <Crop className="h-4 w-4 text-slate-500" aria-hidden />
                    {data.crop.keyframes?.length ?? 0} tracked
                  </p>
                </div>
              </div>

              {data.crop.mode !== 'auto' ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <p className="label">Horizontal focus — {Math.round((data.crop.focus_x ?? 0.5) * 100)}%</p>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.01}
                      value={data.crop.focus_x ?? 0.5}
                      onChange={(event) =>
                        saveClip.mutate({ crop: { ...data.crop, focus_x: Number(event.target.value) } })
                      }
                    />
                  </div>
                  <div>
                    <p className="label">Vertical focus — {Math.round((data.crop.focus_y ?? 0.5) * 100)}%</p>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.01}
                      value={data.crop.focus_y ?? 0.5}
                      onChange={(event) =>
                        saveClip.mutate({ crop: { ...data.crop, focus_y: Number(event.target.value) } })
                      }
                    />
                  </div>
                </div>
              ) : null}

              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="btn-secondary btn-sm"
                  disabled={!trim || saveClip.isPending}
                  onClick={() =>
                    trim && saveClip.mutate({ start_time: trim.start, end_time: trim.end })
                  }
                >
                  <Save className="h-3.5 w-3.5" aria-hidden />
                  Apply trim
                </button>
              </div>
            </div>
          </Card>

          <Card>
            <SectionHeader
              title="Render"
              description="H.264/AAC MP4. Progress comes from the real FFmpeg job."
              action={
                <div className="flex flex-wrap gap-2">
                  {(presets.data ?? []).slice(0, 8).map((preset) => (
                    <button
                      key={preset.key}
                      type="button"
                      className="btn-secondary btn-sm"
                      disabled={render.isPending || Boolean(activeRender)}
                      onClick={() => render.mutate(preset.key)}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
              }
            />

            {activeRender ? (
              <div className="rounded-xl border border-accent-500/25 bg-accent-500/[0.06] px-4 py-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-slate-200">{activeRender.message || activeRender.stage}</span>
                  <span className="tabular-nums text-slate-400">{Math.round(activeRender.progress)}%</span>
                </div>
                <ProgressBar className="mt-2" value={activeRender.progress} />
                <button
                  type="button"
                  className="btn-ghost btn-sm mt-2"
                  onClick={async () => {
                    try {
                      await api.post(`/renders/${activeRender.id}/cancel`);
                      queryClient.invalidateQueries({ queryKey: queryKeys.renders(clipId) });
                    } catch (error) {
                      toast({ kind: 'error', title: 'Could not cancel the render', description: (error as Error).message });
                    }
                  }}
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                  Cancel render
                </button>
              </div>
            ) : latestRender ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-ink-700/70 bg-ink-900/50 px-4 py-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-200">
                    {latestRender.status === 'succeeded' ? 'Render complete' : latestRender.status}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {latestRender.width}×{latestRender.height} · {formatBytes(latestRender.output_size_bytes)} ·{' '}
                    {latestRender.duration_seconds.toFixed(1)}s
                    {latestRender.warnings.length ? ` · ${latestRender.warnings.join(', ')}` : ''}
                  </p>
                </div>
                {latestRender.status === 'succeeded' ? (
                  <a
                    className="btn-secondary btn-sm"
                    href={`/api/v1/renders/${latestRender.id}/download`}
                    download
                  >
                    <Download className="h-3.5 w-3.5" aria-hidden />
                    Download
                  </a>
                ) : null}
              </div>
            ) : (
              <p className="text-sm text-slate-500">
                No render yet. Pick a preset above to queue one — it runs as a background job you can cancel.
              </p>
            )}
          </Card>
        </div>

        <div className="space-y-5">
          <div className="flex gap-1 rounded-xl border border-ink-700/70 bg-ink-900/50 p-1">
            {tabs.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setTab(item.key)}
                className={cn(
                  'flex flex-1 items-center justify-center gap-1.5 rounded-lg px-2 py-2 text-xs font-medium transition-colors',
                  tab === item.key ? 'bg-ink-700 text-white' : 'text-slate-400 hover:text-slate-200',
                )}
              >
                <item.icon className="h-3.5 w-3.5" aria-hidden />
                {item.label}
              </button>
            ))}
          </div>

          {tab === 'suggestions' ? (
            <Card>
              <SectionHeader title="Headline" description="Nothing is placed until you choose it." />
              <input
                className="input"
                value={headlineDraft}
                onChange={(event) => setHeadlineDraft(event.target.value)}
                placeholder="Write a headline"
              />
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  className="btn-secondary btn-sm"
                  onClick={() => generateHeadlines.mutate()}
                  disabled={generateHeadlines.isPending}
                >
                  <Sparkles className="h-3.5 w-3.5" aria-hidden />
                  {generateHeadlines.isPending ? 'Generating…' : 'Suggest headlines'}
                </button>
                <button
                  type="button"
                  className="btn-primary btn-sm"
                  disabled={!headlineDraft || saveClip.isPending}
                  onClick={() => saveClip.mutate({ headline: headlineDraft })}
                >
                  <Save className="h-3.5 w-3.5" aria-hidden />
                  Save
                </button>
              </div>

              {headlines.data?.suggestions?.length ? (
                <ul className="mt-4 space-y-2">
                  {headlines.data.suggestions.map((suggestion) => (
                    <li key={`${suggestion.provider}:${suggestion.text}`}>
                      <button
                        type="button"
                        className="w-full rounded-lg border border-ink-700/70 bg-ink-900/50 px-3 py-2 text-left text-sm text-slate-300 transition-colors hover:border-accent-500/40 hover:text-white"
                        onClick={() => selectHeadline.mutate(suggestion.text)}
                        title={suggestion.source ? `source: ${suggestion.source}` : undefined}
                      >
                        {suggestion.text}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-4 text-xs text-slate-500">
                  {headlines.data?.message ||
                    'Suggestions come from your language model provider, or from the clip transcript when none is configured.'}
                </p>
              )}

              {candidate ? (
                <div className="mt-6 space-y-3 border-t border-ink-700 pt-4">
                  <p className="panel-title">Why this was found</p>
                  <p className="text-sm text-slate-300">{candidate.reason}</p>
                  <div className="space-y-2">
                    {Object.entries(candidate.scores)
                      .filter(([key]) => key !== 'structural')
                      .slice(0, 5)
                      .map(([key, value]) => (
                        <div key={key} className="flex items-center gap-3 text-xs">
                          <span className="w-24 capitalize text-slate-400">{key}</span>
                          <div className="h-1 flex-1 overflow-hidden rounded-full bg-ink-700">
                            <div className="h-full rounded-full bg-accent-400" style={{ width: `${value}%` }} />
                          </div>
                          <span className="w-8 text-right tabular-nums text-slate-400">{Math.round(value)}</span>
                        </div>
                      ))}
                  </div>
                  <p className="text-2xs text-slate-500">Analyzer: {candidate.analyzer}</p>
                </div>
              ) : null}
            </Card>
          ) : null}

          {tab === 'transcript' ? (
            <Card>
              <SectionHeader title="Transcript" description="Click a word to seek the player." />
              <TranscriptTab
                clip={data}
                transcript={transcriptForWords}
                videoRef={videoRef}
                onSeekTime={setCurrentTime}
              />
            </Card>
          ) : null}

          {tab === 'captions' ? (
            <Card>
              <SectionHeader
                title="Captions"
                description={
                  captions.data?.transcript_available
                    ? `${captions.data.words.length} words in this clip`
                    : 'No transcript covers this clip yet.'
                }
              />
              {captions.isPending ? (
                <Skeleton className="h-40 w-full" />
              ) : (
                <CaptionsTab
                  clip={data}
                  captions={captions.data}
                  presets={captionPresets.data?.presets ?? []}
                  saving={saveCaptions.isPending}
                  onSave={(payload) => saveCaptions.mutate(payload)}
                />
              )}
            </Card>
          ) : null}

          <Card>
            <SectionHeader title="Clip details" />
            <dl className="space-y-2 text-sm">
              {[
                ['Duration', `${data.duration.toFixed(2)}s`],
                ['Aspect ratio', data.aspect_ratio],
                ['Framing', `${data.crop.mode ?? 'auto'} (${data.crop.strategy || 'centre'})`],
                ['Renders', String(data.render_count)],
                ['Source', data.source],
                ['Status', data.status],
              ].map(([label, value]) => (
                <div key={label} className="flex items-center justify-between gap-3">
                  <dt className="text-slate-500">{label}</dt>
                  <dd className="truncate text-slate-300">{value}</dd>
                </div>
              ))}
            </dl>
          </Card>

          <button
            type="button"
            className="btn-secondary w-full"
            onClick={() => navigate(`/projects/${projectId}/clips`)}
          >
            <ListVideo className="h-4 w-4" aria-hidden />
            Back to all moments
          </button>
        </div>
      </div>
    </div>
  );
}
