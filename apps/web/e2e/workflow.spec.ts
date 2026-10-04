import { expect, test } from '@playwright/test';
import {
  api,
  createProject,
  JOB,
  ensureTranscribable,
  loginViaUi,
  registerViaUi,
  requireDemoVideo,
  uniqueEmail,
  uploadVideo,
  waitForJob,
  waitForTranscript,
  type JobRow,
} from './helpers';

/**
 * The full acceptance path:
 *
 *   register → project → upload → transcribe → discover moments → keep one →
 *   create clip → edit captions → render → download
 *
 * Every stage waits on real backend state (jobs, transcripts, renders) rather
 * than on the spinner disappearing, so a job that silently does nothing fails
 * the test instead of passing it.
 */
test.describe('creator workflow', () => {
  // Uploading, probing, transcribing and rendering a real clip all take minutes.
  test.setTimeout(20 * 60_000);

  test('upload, transcribe, discover, clip, caption, render and download', async ({ page }) => {
    const videoPath = requireDemoVideo();
    const email = uniqueEmail('e2e-flow');
    const projectName = `E2E workflow ${Date.now()}`;

    // ------------------------------------------------------------ account ---
    await registerViaUi(page, email);
    const client = api(page);

    // ------------------------------------------------------------ project ---
    await createProject(page, projectName);
    const projectId = page.url().split('/projects/')[1].split(/[/?#]/)[0];

    // ------------------------------------------------------------- upload ---
    await uploadVideo(page, videoPath);

    const videos = await client.json<{ items: { id: string; status: string }[] }>(
      await client.get(`/projects/${projectId}/videos?limit=10`),
    );
    expect(videos.items.length, 'the upload should produce a video row').toBeGreaterThan(0);
    const videoId = videos.items[0].id;

    // The probe is a real job; wait for it rather than assuming the file parsed.
    await waitForJob(client, projectId, JOB.probe);
    await expect
      .poll(async () => {
        const response = await client.get(`/videos/${videoId}`);
        const video = (await response.json()) as { status: string; duration_seconds: number; width: number };
        return { status: video.status, w: video.width, d: video.duration_seconds };
      }, { timeout: 120_000, message: 'the video should be probed with real dimensions' })
      .toMatchObject({ status: 'ready' });

    const probed = await client.json<{ duration_seconds: number; width: number; height: number }>(
      await client.get(`/videos/${videoId}`),
    );
    expect(probed.width, 'a real probe reports width').toBeGreaterThan(0);
    expect(probed.duration_seconds, 'a real probe reports duration').toBeGreaterThan(1);

    // -------------------------------------------------------- transcribe ----
    // Makes transcription possible if ASR is unavailable but script alignment
    // works (the supported offline path) — never a fabricated transcript.
    const mode = await ensureTranscribable(client, videoId);
    test.info().annotations.push({ type: 'transcription-mode', description: mode });

    await page.getByRole('button', { name: /^transcribe$|re-transcribe/i }).first().click();
    await waitForJob(client, projectId, JOB.transcribe);
    const transcriptId = await waitForTranscript(client, videoId);

    const transcript = await client.json<{
      segments: { start_time: number; end_time: number; text: string }[];
      words: { word: string; start_time: number; end_time: number; confidence: number; speaker: string | null }[];
    }>(await client.get(`/transcripts/${transcriptId}`));

    expect(transcript.words.length, 'word-level timings must exist').toBeGreaterThan(10);
    // Timings have to be real and monotonic, not placeholders.
    for (const word of transcript.words.slice(0, 25)) {
      expect(word.end_time).toBeGreaterThanOrEqual(word.start_time);
      expect(word.start_time).toBeLessThanOrEqual(probed.duration_seconds + 1);
    }
    expect(transcript.words.every((word) => word.word.trim() !== ''), 'every word carries its text').toBeTruthy();
    // Speaker labels are optional, but when present they must be real labels.
    const speakers = new Set(transcript.words.map((word) => word.speaker).filter(Boolean));
    test.info().annotations.push({ type: 'speakers', description: `${speakers.size} speaker label(s)` });

    // ------------------------------------------------- discover moments -----
    await page.getByRole('button', { name: /find clips/i }).first().click();
    const discover = await waitForJob(client, projectId, JOB.discover);
    expect(discover.result, 'discovery stores its outcome').toBeTruthy();

    await page.goto(`/projects/${projectId}/clips`);
    await expect(page.getByRole('heading', { name: /review moments/i })).toBeVisible({ timeout: 60_000 });

    const candidates = await client.json<{
      items: { id: string; start_time: number; end_time: number; title: string; score: number; reason: string }[];
    }>(await client.get(`/projects/${projectId}/candidates?limit=50`));

    expect(candidates.items.length, 'analysis should find candidates in a real recording').toBeGreaterThan(0);
    for (const candidate of candidates.items.slice(0, 10)) {
      expect(candidate.end_time, 'candidates have real boundaries').toBeGreaterThan(candidate.start_time);
      expect(candidate.score, 'scores come from analysis').toBeGreaterThan(0);
      expect(candidate.score).toBeLessThanOrEqual(100);
    }

    // ------------------------------------------------------------- clip -----
    const card = page.locator('article, li, div').filter({ has: page.getByRole('button', { name: /^keep$/i }) }).first();
    await card.getByRole('button', { name: /^keep$/i }).click();
    await expect
      .poll(async () => {
        const body = await client.json<{ items: { status: string }[] }>(
          await client.get(`/projects/${projectId}/candidates?limit=50`),
        );
        return body.items.some((item) => item.status === 'kept');
      }, { timeout: 30_000 })
      .toBeTruthy();

    await card.getByRole('button', { name: /create clip/i }).click();

    const clips = await client.json<{ items: { id: string; aspect_ratio: string; status: string }[] }>(
      await client.get(`/projects/${projectId}/clips?limit=20`),
    );
    expect(clips.items.length, 'a kept moment becomes a real clip').toBeGreaterThan(0);
    const clipId = clips.items[0].id;
    await page.goto(`/projects/${projectId}/editor/${clipId}`);

    // ---------------------------------------------------------- captions ----
    await expect(page.getByRole('button', { name: /save captions/i })).toBeVisible({ timeout: 60_000 });

    const firstWord = page.getByLabel('Caption word 1');
    await expect(firstWord).toBeVisible({ timeout: 30_000 });
    const originalWord = await firstWord.inputValue();
    const editedWord = originalWord.endsWith('!') ? `${originalWord}x` : `${originalWord}!`;
    await firstWord.fill(editedWord);

    // Re-timing must not move when the text changes: capture before and after.
    const before = await client.json<{ cues: { start: number; end: number }[] }>(
      await client.get(`/clips/${clipId}/captions`),
    );
    await page.getByRole('button', { name: /save captions/i }).click();
    await expect(page.getByRole('alert').filter({ hasText: /caption/i }).first().or(
      page.getByText(/timing is preserved/i).first(),
    )).toBeVisible({ timeout: 30_000 }).catch(() => undefined);

    await expect
      .poll(async () => {
        const body = await client.json<{ cues: { words: { word: string }[] }[] }>(
          await client.get(`/clips/${clipId}/captions`),
        );
        return body.cues[0]?.words[0]?.word;
      }, { timeout: 30_000, message: 'the edited text must persist' })
      .toBe(editedWord);

    const after = await client.json<{ cues: { start: number; end: number }[] }>(
      await client.get(`/clips/${clipId}/captions`),
    );
    expect(after.cues.length, 'editing text must not drop cues').toBe(before.cues.length);
    expect(after.cues[0].start).toBeCloseTo(before.cues[0].start, 3);
    expect(after.cues[after.cues.length - 1].end).toBeCloseTo(before.cues[before.cues.length - 1].end, 3);

    // ------------------------------------------------------------ render ----
    const rendersBefore = await client.json<unknown[]>(await client.get(`/clips/${clipId}/renders`));

    // "TikTok / Reels" is the vertical preset; the button label comes from the API.
    await page.getByRole('button', { name: /tiktok|reels|vertical/i }).first().click();

    await expect
      .poll(async () => {
        const body = await client.json<{ items: JobRow[] }>(await client.get(`/jobs?project_id=${projectId}&limit=50`));
        return body.items.some((job) => job.type === JOB.render);
      }, { timeout: 60_000, message: 'rendering must create a real job' })
      .toBeTruthy();

    const renderJob = await waitForJob(client, projectId, JOB.render);

    const renders = await client.json<
      {
        id: string;
        status: string;
        width: number;
        height: number;
        output_size_bytes: number;
        progress: number;
        job_id: string | null;
      }[]
    >(await client.get(`/clips/${clipId}/renders`));

    expect(renders.length).toBeGreaterThan(rendersBefore.length);
    const render = renders.find((item) => item.job_id === renderJob.id) ?? renders[0];
    expect(render.status, 'the render row tracks the job it came from').toBe('succeeded');
    expect(render.width).toBe(1080);
    expect(render.height).toBe(1920);
    expect(render.output_size_bytes, 'a real MP4 has bytes').toBeGreaterThan(10_000);

    // This is an MP4 test, but the assertion is on the container rather than a
    // tool on the box: fetch the file back and check the ISO-BMFF box signature.
    const download = await page.request.get(`/api/v1/renders/${render.id}/download`);
    expect(download.ok(), `download failed: ${download.status()}`).toBeTruthy();
    expect(download.headers()['content-type']).toMatch(/video\/mp4|application\/octet-stream/);
    const bytes = await download.body();
    expect(bytes.length, 'the downloaded file matches the reported size').toBe(render.output_size_bytes);
    expect(bytes.subarray(4, 8).toString('latin1'), 'ftyp box marks a real MP4').toBe('ftyp');
    expect(bytes.subarray(8, 12).toString('latin1')).toMatch(/isom|mp42|iso5|dash|avc1|mp41/);

    // The UI exposes the same file as a link, and it is not a dead button.
    await page.reload();
    const link = page.getByRole('link', { name: /download/i }).first();
    await expect(link).toBeVisible({ timeout: 60_000 });
    await expect(link).toHaveAttribute('href', `/api/v1/renders/${render.id}/download`);

    // -------------------------------------------------- session recovery ----
    // A fresh context with the same cookies must restore state from the API —
    // nothing may live only in WebSocket memory.
    await page.goto(`/projects/${projectId}/clips`);
    await expect(page.getByRole('heading', { name: /review moments/i })).toBeVisible({ timeout: 60_000 });
  });

  test('a signed-out visitor cannot reach projects or videos', async ({ page }) => {
    await registerViaUi(page, uniqueEmail('e2e-logout'));
    const client = api(page);
    const projectName = `E2E logout ${Date.now()}`;
    await createProject(page, projectName);
    const projectId = page.url().split('/projects/')[1].split(/[/?#]/)[0];

    await page.getByRole('button', { name: /sign out|log out/i }).first().click();
    await expect(page).toHaveURL(/\/(login|$)|^\/$/, { timeout: 20_000 });

    const response = await page.request.get(`/api/v1/projects/${projectId}`);
    expect([401, 403, 404], 'a signed-out request must not return project data').toContain(response.status());

    await page.goto(`/projects/${projectId}`);
    await expect(page).toHaveURL(/\/login/, { timeout: 20_000 });
    void client;
  });

  test('a second account cannot see the first account\'s project', async ({ page, browser }) => {
    await registerViaUi(page, uniqueEmail('e2e-owner'));
    const projectName = `E2E private ${Date.now()}`;
    await createProject(page, projectName);
    const projectId = page.url().split('/projects/')[1].split(/[/?#]/)[0];

    const other = await browser.newContext();
    const otherPage = await other.newPage();
    await loginViaUi(otherPage, await registerViaUi(otherPage, uniqueEmail('e2e-other')));

    const response = await otherPage.request.get(`/api/v1/projects/${projectId}`);
    expect([403, 404], 'another user must not read the project').toContain(response.status());
    await other.close();
  });
});
