/**
 * API response types, mirroring `apps/api/app/schemas`.
 *
 * These are hand-written rather than generated so the shapes stay readable;
 * `npm run check:api` (scripts/check_api_types.py) fails the build if the server
 * adds or renames a field these types do not know about.
 */

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  status: string;
  target_aspect_ratio: string;
  privacy_mode: string;
  source_language: string;
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  caption_style_id: string | null;
}

export interface ProjectSummary extends Project {
  video_count: number;
  clip_count: number;
  candidate_count: number;
  render_count: number;
  total_duration_seconds: number;
  latest_video: Video | null;
  active_jobs: Job[];
  progress: number;
  stage: string;
}

export interface Video {
  id: string;
  project_id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  status: string;
  duration_seconds: number;
  width: number;
  height: number;
  fps: number;
  video_codec: string;
  audio_codec: string;
  has_audio: boolean;
  thumbnail_key: string | null;
  storage_key: string;
  error_message: string;
  probe: Record<string, unknown>;
  created_at: string;
}

export interface Job {
  id: string;
  type: string;
  status: string;
  stage: string;
  progress: number;
  message: string;
  project_id: string | null;
  video_id: string | null;
  clip_id: string | null;
  attempts: number;
  max_attempts: number;
  error_code: string;
  error_message: string;
  result: Record<string, unknown>;
  payload: Record<string, unknown>;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number;
}

export interface TranscriptWord {
  id: string;
  index: number;
  word: string;
  start_time: number;
  end_time: number;
  confidence: number;
  speaker: string;
  is_edited: boolean;
}

export interface TranscriptSegment {
  id: string;
  index: number;
  start_time: number;
  end_time: number;
  text: string;
  speaker: string;
  avg_confidence: number;
  word_count: number;
  energy: number;
  energy_variance: number;
  pause_before: number;
  speech_rate: number;
}

export interface Transcript {
  id: string;
  video_id: string;
  project_id: string;
  status: string;
  provider: string;
  model: string;
  language: string;
  language_probability: number;
  duration_seconds: number;
  segment_count: number;
  word_count: number;
  speaker_count: number;
  avg_confidence: number;
  full_text: string;
  error_message: string;
  meta: Record<string, unknown>;
  created_at: string;
  segments?: TranscriptSegment[];
  words?: TranscriptWord[];
}

export interface Candidate {
  id: string;
  project_id: string;
  video_id: string;
  start_time: number;
  end_time: number;
  duration: number;
  title: string;
  hook: string;
  reason: string;
  score: number;
  scores: Record<string, number>;
  keywords: string[];
  category: string;
  transcript_text: string;
  speaker: string;
  status: string;
  analyzer: string;
  rank: number;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface CandidateScoreDetail {
  candidate_id: string;
  score: number;
  components: Record<string, number>;
  weights: Record<string, number>;
  analyzer: string;
  explanation: string;
}

export interface CropKeyframe {
  time: number;
  center_x: number;
  center_y: number;
  width: number;
  height: number;
  confidence: number;
  source: string;
}

export interface Clip {
  id: string;
  project_id: string;
  video_id: string;
  candidate_id: string | null;
  title: string;
  headline: string;
  headline_position: string;
  headline_style: Record<string, unknown>;
  headline_variants: string[];
  start_time: number;
  end_time: number;
  duration: number;
  aspect_ratio: string;
  crop: {
    mode?: string;
    focus_x?: number;
    focus_y?: number;
    zoom?: number;
    strategy?: string;
    detector?: string;
    keyframes?: CropKeyframe[];
  };
  background: Record<string, unknown>;
  caption_config: Record<string, unknown>;
  caption_overrides: Record<string, unknown>;
  audio_config: Record<string, unknown>;
  caption_style_id: string | null;
  status: string;
  source: string;
  is_favorite: boolean;
  render_count: number;
  last_rendered_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClipDetail extends Clip {
  video: Record<string, unknown>;
  candidate: Candidate | null;
  latest_render: Render | null;
}

export interface CaptionWord {
  index: number;
  word: string;
  start: number;
  end: number;
  speaker: string;
  is_edited: boolean;
}

export interface CaptionCue {
  start: number;
  end: number;
  text: string;
  word_count: number;
  line_count: number;
  words: CaptionWord[];
}

export interface CaptionResponse {
  clip_id: string;
  enabled: boolean;
  style: Record<string, unknown>;
  style_name: string;
  cues: CaptionCue[];
  words: CaptionWord[];
  transcript_available: boolean;
  message: string;
}

export interface CaptionStyle {
  id: string;
  name: string;
  preset_key: string;
  description: string;
  is_system: boolean;
  is_favorite: boolean;
  config: Record<string, unknown>;
  user_id: string | null;
}

export interface HeadlineSuggestion {
  text: string;
  source: string;
  provider: string;
}

export interface HeadlineResponse {
  clip_id: string;
  current: string;
  suggestions: HeadlineSuggestion[];
  provider: string;
  message: string;
}

export interface AIProvider {
  id: string;
  kind: string;
  provider: string;
  label: string;
  model: string;
  base_url: string;
  is_default: boolean;
  is_enabled: boolean;
  has_api_key: boolean;
  masked_api_key: string;
  created_at: string;
}

export interface Render {
  id: string;
  clip_id: string;
  project_id: string;
  job_id: string | null;
  preset: string;
  aspect_ratio: string;
  width: number;
  height: number;
  fps: number;
  video_codec: string;
  audio_codec: string;
  bitrate: string;
  status: string;
  progress: number;
  stage: string;
  message: string;
  storage_key: string;
  thumbnail_key: string;
  output_size_bytes: number;
  output_duration: number;
  warnings: string[];
  error_code: string;
  error_message: string;
  attempts: number;
  spec: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number;
}

export interface RenderPreset {
  key: string;
  label: string;
  width: number;
  height: number;
  aspect_ratio: string;
  fps: number;
  bitrate: string;
  max_seconds: number;
}

export interface ExportItem {
  id: string;
  render_id: string;
  clip_id: string;
  project_id: string;
  platform: string;
  filename: string;
  storage_key: string;
  thumbnail_key: string;
  size_bytes: number;
  duration_seconds: number;
  width: number;
  height: number;
  download_count: number;
  created_at: string;
  last_downloaded_at: string | null;
  clip_title?: string;
  clip_headline?: string;
}

export interface Plan {
  key: string;
  name: string;
  tagline: string;
  price_minor: number;
  currency: string;
  interval: string;
  features: string[];
  highlighted: boolean;
  current: boolean;
  limits: {
    minutes_processed: number;
    videos_uploaded: number;
    clips_generated: number;
    renders: number;
    storage_bytes: number;
    ai_requests: number;
    max_upload_bytes: number;
    max_video_minutes: number;
    priority_queue: boolean;
  };
}

export interface UsageSummary {
  plan: string;
  metrics: Record<string, { used: number; limit: number; unit: string; percent: number }>;
  period_start?: string;
  period_end?: string;
}

export interface Capabilities {
  ffmpeg: boolean;
  ffprobe: boolean;
  media_pipeline: boolean;
  vision: {
    enabled: boolean;
    active: string;
    face_detection: boolean;
    detectors: Array<{ name: string; available: boolean; detail?: string }>;
  };
  transcription: {
    local: { name: string; installed: boolean; available: boolean; model: string; detail: string };
    api: { name: string; available: boolean; model: string };
    active: string;
    script_alignment?: { available: boolean; name: string; detail: string; is_local: boolean };
  };
  llm: {
    providers: Array<{ name: string; available: boolean; model: string; detailed?: string; is_local?: boolean }>;
    configured: string[];
    active: string;
  };
  embeddings: { neural_available: boolean; active: string; note: string };
  storage_backend: string;
  queue_backend: string;
  billing: Array<{ name: string; available: boolean }>;
  processor: { enabled: boolean; url: string };
  environment: string;
}
