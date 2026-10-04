import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** `83.4` -> `1:23` (timeline / clip durations). */
export function formatTime(seconds: number, options: { hours?: boolean; tenths?: boolean } = {}): string {
  const value = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = Math.floor(value % 60);
  const parts: string[] = [];
  if (hours > 0 || options.hours) parts.push(String(hours));
  parts.push(hours > 0 || options.hours ? String(minutes).padStart(2, '0') : String(minutes));
  parts.push(String(secs).padStart(2, '0'));
  const base = parts.join(':');
  if (!options.tenths) return base;
  const tenth = Math.floor((value % 1) * 10);
  return `${base}.${tenth}`;
}

export function formatDuration(seconds: number): string {
  const value = Math.max(0, Math.round(seconds || 0));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const secs = value % 60;
  if (minutes < 60) return secs ? `${minutes}m ${secs}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function formatBytes(bytes: number): string {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
}

export function formatRelative(value: string | Date | null | undefined): string {
  if (!value) return '';
  const date = typeof value === 'string' ? new Date(value) : value;
  const diff = Date.now() - date.getTime();
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export function formatDateTime(value: string | Date | null | undefined): string {
  if (!value) return '';
  const date = typeof value === 'string' ? new Date(value) : value;
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function percent(value: number, total: number): number {
  if (!total) return 0;
  return clamp((value / total) * 100, 0, 100);
}

/** Human labels for job types / stages shown in progress UI. */
export const JOB_LABELS: Record<string, string> = {
  'video.probe': 'Inspecting media',
  'video.transcribe': 'Transcribing',
  'video.analyze_framing': 'Analysing framing',
  'project.discover_clips': 'Finding moments',
  'clip.render': 'Rendering clip',
};

export function jobLabel(type: string): string {
  return JOB_LABELS[type] || type.replace(/[._]/g, ' ');
}

export const ASPECT_LABELS: Record<string, string> = {
  '9:16': 'Vertical 9:16',
  '1:1': 'Square 1:1',
  '16:9': 'Landscape 16:9',
};

export function titleCase(value: string): string {
  return value
    .replace(/[_-]/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase());
}
