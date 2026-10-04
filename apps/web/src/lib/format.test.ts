/** Formatting helpers: the numbers a creator actually reads. */
import { describe, expect, it } from 'vitest';
import { clamp, cn, formatBytes, formatDateTime, formatDuration, formatTime, jobLabel, percent, titleCase } from '@/lib/format';

describe('formatTime', () => {
  it('formats mm:ss by default', () => {
    expect(formatTime(0)).toBe('0:00');
    expect(formatTime(9)).toBe('0:09');
    expect(formatTime(65)).toBe('1:05');
    expect(formatTime(600)).toBe('10:00');
  });

  it('adds hours once the value passes an hour', () => {
    expect(formatTime(3661, { hours: true })).toBe('1:01:01');
    expect(formatTime(3661)).toBe('1:01:01');
    expect(formatTime(3599)).toBe('59:59');
  });

  it('never renders a negative time', () => {
    expect(formatTime(-5)).toBe('0:00');
  });

  it('keeps sub-second precision when requested', () => {
    expect(formatTime(1.25, { tenths: true })).toBe('0:01.2');
  });
});

describe('formatDuration', () => {
  it('describes short clips in whole seconds', () => {
    expect(formatDuration(39.85)).toBe('40s');
    expect(formatDuration(9)).toBe('9s');
  });

  it('describes long videos in minutes', () => {
    expect(formatDuration(143.37)).toMatch(/2:23|143|2m/);
  });

  it('handles zero and missing values', () => {
    expect(formatDuration(0)).toBeTruthy();
    expect(formatDuration(Number.NaN)).toBeTruthy();
  });
});

describe('formatBytes', () => {
  it('scales units', () => {
    expect(formatBytes(512)).toMatch(/512\s?B/);
    expect(formatBytes(2048)).toMatch(/2(\.0)?\s?KB/);
    expect(formatBytes(10 * 1024 * 1024)).toMatch(/10(\.0)?\s?MB/);
    expect(formatBytes(3 * 1024 ** 3)).toMatch(/3(\.0)?\s?GB/);
  });

  it('is stable for edge inputs', () => {
    expect(formatBytes(0)).toBeTruthy();
    expect(formatBytes(-1)).toBeTruthy();
  });
});

describe('formatDateTime', () => {
  it('renders a readable timestamp', () => {
    expect(formatDateTime('2026-01-02T03:04:05Z')).toMatch(/Jan/);
  });

  it('returns an empty string for missing or invalid input so callers can fall back', () => {
    expect(formatDateTime(null)).toBe('');
    expect(formatDateTime(undefined)).toBe('');
    expect(formatDateTime('not-a-date')).toBe('');
  });
});

describe('clamp and percent', () => {
  it('clamps into range', () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-5, 0, 10)).toBe(0);
    expect(clamp(50, 0, 10)).toBe(10);
  });

  it('never divides by zero', () => {
    expect(percent(5, 0)).toBe(0);
    expect(percent(0, 0)).toBe(0);
    expect(percent(1, 4)).toBe(25);
  });
});

describe('jobLabel', () => {
  it('turns job types into readable labels', () => {
    expect(jobLabel('transcribe')).toMatch(/transcri/i);
    expect(jobLabel('discover')).toMatch(/discov/i);
    expect(jobLabel('render')).toMatch(/render/i);
    expect(jobLabel('framing')).toMatch(/fram/i);
  });

  it('never returns an empty label for an unknown type', () => {
    expect(jobLabel('mystery_job')).toBeTruthy();
  });
});

describe('titleCase', () => {
  it('title-cases snake_case and single words', () => {
    expect(titleCase('high_contrast')).toBe('High Contrast');
    expect(titleCase('minutes_processed')).toBe('Minutes Processed');
    expect(titleCase('pro')).toBe('Pro');
  });
});

describe('cn', () => {
  it('merges conditional classes and resolves conflicts', () => {
    expect(cn('p-2', false && 'hidden', undefined, 'text-white')).toBe('p-2 text-white');
    // tailwind-merge must keep the last conflicting utility.
    expect(cn('p-2', 'p-4')).toBe('p-4');
  });
});
