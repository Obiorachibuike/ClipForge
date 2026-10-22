import { useId, useState } from 'react';
import { Scissors } from 'lucide-react';
import { cn } from '@/lib/format';

/**
 * Clip-discovery settings.
 *
 * The backend validates and re-clamps these values; the client enforces the
 * same bounds so a typo produces a clear message instead of a rejected job.
 * Defaults match `DiscoverRequest` on the server.
 */
export const DISCOVERY_LIMITS = {
  maxCandidates: 30,
  minDuration: 3,
  // Server-side `DiscoverRequest` caps the *minimum* at 180s even though the
  // maximum clip length may be 300s. Mirroring that here avoids a 422 for a
  // value the client would otherwise consider valid.
  minDurationMax: 180,
  maxDuration: 300,
} as const;

export interface DiscoveryOptions {
  max_candidates: number;
  min_duration: number;
  max_duration: number;
}

export const DEFAULT_DISCOVERY: DiscoveryOptions = {
  max_candidates: 12,
  min_duration: 10,
  max_duration: 95,
};

function toNumber(value: string, fallback: number): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function clampDiscovery(options: DiscoveryOptions): DiscoveryOptions {
  const maxCandidates = Math.max(
    1,
    Math.min(DISCOVERY_LIMITS.maxCandidates, Math.round(options.max_candidates)),
  );
  const minDuration = Math.max(
    DISCOVERY_LIMITS.minDuration,
    Math.min(DISCOVERY_LIMITS.minDurationMax, options.min_duration),
  );
  const maxDuration = Math.max(
    DISCOVERY_LIMITS.minDuration,
    Math.min(DISCOVERY_LIMITS.maxDuration, options.max_duration),
  );
  return {
    max_candidates: maxCandidates,
    min_duration: Math.round(minDuration * 10) / 10,
    max_duration: Math.round(maxDuration * 10) / 10,
  };
}

export function discoveryError(options: DiscoveryOptions): string | null {
  if (!Number.isFinite(options.max_candidates) || options.max_candidates < 1) {
    return 'Choose at least one clip.';
  }
  if (options.max_candidates > DISCOVERY_LIMITS.maxCandidates) {
    return `At most ${DISCOVERY_LIMITS.maxCandidates} clips can be requested at once.`;
  }
  if (!Number.isFinite(options.min_duration) || options.min_duration < DISCOVERY_LIMITS.minDuration) {
    return `Clips must be at least ${DISCOVERY_LIMITS.minDuration} seconds long.`;
  }
  if (options.min_duration > DISCOVERY_LIMITS.minDurationMax) {
    return `The minimum length cannot exceed ${DISCOVERY_LIMITS.minDurationMax} seconds.`;
  }
  if (!Number.isFinite(options.max_duration) || options.max_duration > DISCOVERY_LIMITS.maxDuration) {
    return `Clips cannot exceed ${DISCOVERY_LIMITS.maxDuration} seconds.`;
  }
  if (options.min_duration >= options.max_duration) {
    return 'The minimum length must be shorter than the maximum.';
  }
  return null;
}

export default function DiscoveryControls({
  onStart,
  pending = false,
  disabled = false,
  compact = false,
}: {
  onStart: (options: DiscoveryOptions) => void;
  pending?: boolean;
  disabled?: boolean;
  compact?: boolean;
}) {
  // Keep the raw text per field. Parsing on every keystroke and rendering the
  // parsed number makes a controlled number input impossible to clear: an empty
  // field snaps back to the default, so typing appends to it ("10" -> "10120").
  const [raw, setRaw] = useState<Record<keyof DiscoveryOptions, string>>(() => ({
    max_candidates: String(DEFAULT_DISCOVERY.max_candidates),
    min_duration: String(DEFAULT_DISCOVERY.min_duration),
    max_duration: String(DEFAULT_DISCOVERY.max_duration),
  }));
  const id = useId();

  const parsed: DiscoveryOptions = {
    max_candidates: toNumber(raw.max_candidates, Number.NaN),
    min_duration: toNumber(raw.min_duration, Number.NaN),
    max_duration: toNumber(raw.max_duration, Number.NaN),
  };
  const error = discoveryError(parsed);
  const canSubmit = !disabled && !pending && error === null;

  const update = (key: keyof DiscoveryOptions, value: string) => {
    setRaw((current) => ({ ...current, [key]: value }));
  };

  return (
    <div className={cn('rounded-xl border border-ink-700/70 bg-ink-900/50 p-3', compact && 'p-2.5')}>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-2xs font-semibold uppercase tracking-wider text-slate-500">
          Clips
          <input
            aria-label="Number of clips"
            type="number"
            min={1}
            max={DISCOVERY_LIMITS.maxCandidates}
            value={raw.max_candidates}
            onChange={(event) => update('max_candidates', event.target.value)}
            className="input w-20 py-1 text-sm font-normal normal-case tracking-normal"
            disabled={disabled || pending}
          />
        </label>
        <label className="flex flex-col gap-1 text-2xs font-semibold uppercase tracking-wider text-slate-500">
          Min seconds
          <input
            aria-label="Minimum clip duration"
            type="number"
            min={DISCOVERY_LIMITS.minDuration}
            max={DISCOVERY_LIMITS.minDurationMax}
            step={1}
            value={raw.min_duration}
            onChange={(event) => update('min_duration', event.target.value)}
            className="input w-24 py-1 text-sm font-normal normal-case tracking-normal"
            disabled={disabled || pending}
          />
        </label>
        <label className="flex flex-col gap-1 text-2xs font-semibold uppercase tracking-wider text-slate-500">
          Max seconds
          <input
            aria-label="Maximum clip duration"
            type="number"
            min={DISCOVERY_LIMITS.minDuration}
            max={DISCOVERY_LIMITS.maxDuration}
            step={1}
            value={raw.max_duration}
            onChange={(event) => update('max_duration', event.target.value)}
            className="input w-24 py-1 text-sm font-normal normal-case tracking-normal"
            disabled={disabled || pending}
          />
        </label>
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={!canSubmit}
          onClick={() => onStart(clampDiscovery(parsed))}
        >
          <Scissors className="h-3.5 w-3.5" aria-hidden />
          {pending ? 'Starting…' : 'Find clips'}
        </button>
      </div>
      {error ? (
        <p id={`${id}-error`} className="mt-2 text-2xs text-amber-300">
          {error}
        </p>
      ) : (
        <p className="mt-2 text-2xs text-slate-500">
          Only the strongest non-overlapping moments are returned; fewer than requested is an honest result.
        </p>
      )}
    </div>
  );
}
