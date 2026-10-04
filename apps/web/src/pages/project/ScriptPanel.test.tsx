/**
 * The narration script panel.
 *
 * Its whole job is to make the offline transcription path reachable, so the
 * tests check that it talks to the real endpoints, keeps its wording honest,
 * and never implies a transcript exists before one does.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ScriptPanel from '@/pages/project/ScriptPanel';
import { installFetchDouble, renderWithProviders, type FetchDouble } from '@/test/utils';
import { useUiStore } from '@/stores/ui';
import type { Video } from '@/types/api';

const video: Video = {
  id: 'vid-1',
  project_id: 'prj-1',
  original_filename: 'take.mp4',
  content_type: 'video/mp4',
  size_bytes: 1024,
  status: 'ready',
  duration_seconds: 143,
  width: 1920,
  height: 1080,
  fps: 30,
  video_codec: 'h264',
  audio_codec: 'aac',
  has_audio: true,
  thumbnail_key: null,
  storage_key: 'videos/vid-1.mp4',
  error_message: '',
  probe: {},
  has_narration_script: false,
  created_at: '2026-01-01T00:00:00Z',
};

let double: FetchDouble;

beforeEach(() => {
  double = installFetchDouble();
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
});

afterEach(() => {
  double.restore();
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
});

function scriptResponse(body: Partial<Record<string, unknown>> = {}) {
  return {
    video_id: 'vid-1',
    narration_script: '',
    has_narration_script: false,
    character_count: 0,
    word_count: 0,
    ...body,
  };
}

describe('ScriptPanel', () => {
  it('explains that alignment is not speech recognition', async () => {
    double.respond(/narration-script/, { body: scriptResponse() });
    renderWithProviders(<ScriptPanel video={video} projectId="prj-1" />);

    // The honest caveat has to be on screen, not just in the docs.
    await waitFor(() => {
      expect(screen.getByText(/not a transcript of anything you left out/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/no speech model required/i)).toBeInTheDocument();
  });

  it('will not submit an empty or unchanged script', async () => {
    double.respond(/narration-script/, { body: scriptResponse() });
    renderWithProviders(<ScriptPanel video={video} projectId="prj-1" />);

    const save = await screen.findByRole('button', { name: /save script/i });
    expect(save).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/script text/i), 'One real sentence.');
    expect(await screen.findByText(/unsaved changes/i)).toBeInTheDocument();
    expect(save).toBeEnabled();
  });

  it('saves through PATCH and reports the word count it will align', async () => {
    double.respond(/narration-script/, { body: scriptResponse() });
    double.respond(/\/videos\/vid-1$/, { body: { ...video, has_narration_script: true } });
    renderWithProviders(<ScriptPanel video={video} projectId="prj-1" />);

    const box = await screen.findByLabelText(/script text/i);
    await userEvent.type(box, 'Create every single day and publish it.');
    expect(await screen.findByText(/7 words/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /save script/i }));

    await waitFor(() => {
      const patch = double.calls.find((call) => call.method === 'PATCH');
      expect(patch, 'saving must be a real PATCH').toBeTruthy();
      expect(patch!.url).toContain('/videos/vid-1');
      expect(patch!.body).toMatchObject({ narration_script: 'Create every single day and publish it.' });
    });
  });

  it('loads an existing script and offers to remove it', async () => {
    double.respond(/narration-script/, {
      body: scriptResponse({ narration_script: 'Stored words here.', has_narration_script: true, word_count: 3 }),
    });
    renderWithProviders(<ScriptPanel video={{ ...video, has_narration_script: true }} projectId="prj-1" />);

    const box = await screen.findByLabelText(/script text/i);
    await waitFor(() => expect(box).toHaveValue('Stored words here.'));
    // The panel shows "saved" as a status badge and in the word-count line.
    expect(screen.getAllByText(/saved/i).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /remove script/i })).toBeEnabled();

    double.respond(/\/videos\/vid-1$/, { body: { ...video, has_narration_script: false } });
    await userEvent.click(screen.getByRole('button', { name: /remove script/i }));

    await waitFor(() => {
      const patch = double.calls.find((call) => call.method === 'PATCH' && call.body);
      expect(patch!.body).toMatchObject({ narration_script: '' });
    });
  });

  it('surfaces a failed save as an error toast instead of pretending it worked', async () => {
    double.respond(/narration-script/, { body: scriptResponse() });
    double.respond(/\/videos\/vid-1$/, {
      status: 500,
      body: { code: 'server_error', message: 'Something went wrong on our side.' },
    });
    renderWithProviders(<ScriptPanel video={video} projectId="prj-1" />);

    await userEvent.type(await screen.findByLabelText(/script text/i), 'Words that will not save.');
    await userEvent.click(screen.getByRole('button', { name: /save script/i }));

    await waitFor(() => {
      const toasts = useUiStore.getState().toasts;
      expect(toasts.some((toast) => toast.kind === 'error')).toBe(true);
    });
  });
});
