import { expect, type APIResponse, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

/** Absolute path to a demo clip the workflow spec uploads. */
export const DEMO_VIDEO =
  process.env.E2E_VIDEO ?? path.resolve(__dirname, '..', '..', '..', 'demo', 'creator_masterclass.mp4');
export const DEMO_NARRATION = path.resolve(__dirname, '..', '..', '..', 'demo', 'narration.txt');

/** Unique account per run so repeated runs never collide on an email. */
export function uniqueEmail(prefix = 'e2e'): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
}

export const PASSWORD = 'e2e-password-1';

/**
 * The demo asset is generated, not committed:
 *   python scripts/make_demo_asset.py --out demo
 * Failing with that instruction beats a confusing failure inside the pipeline.
 */
export function requireDemoVideo(): string {
  try {
    readFileSync(DEMO_VIDEO);
    return DEMO_VIDEO;
  } catch {
    throw new Error(
      `No demo video at ${DEMO_VIDEO}. Generate one first:\n  python scripts/make_demo_asset.py --out demo`,
    );
  }
}

function readNarration(): string {
  try {
    return readFileSync(DEMO_NARRATION, 'utf8');
  } catch {
    return '';
  }
}

/** Register through the UI and land on the dashboard. */
export async function registerViaUi(page: Page, email = uniqueEmail()): Promise<string> {
  await page.goto('/register');
  await page.getByLabel('Name').fill('E2E Creator');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: /create account|sign up|register/i }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
  return email;
}

/** Sign in through the UI. */
export async function loginViaUi(page: Page, email: string, password = PASSWORD): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: /sign in|log in/i }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
}

/** Create a project from the dashboard, then wait to land on its page. */
export async function createProject(page: Page, name: string): Promise<void> {
  await page.getByRole('button', { name: /new project/i }).first().click();
  await page.getByLabel('Project name').fill(name);
  await page.getByRole('button', { name: /create project/i }).click();
  await expect(page).toHaveURL(/\/projects\/[0-9a-f]+/, { timeout: 30_000 });
  await expect(page.getByRole('heading', { name })).toBeVisible({ timeout: 30_000 });
}

/** Upload a video through the file input on the project page. */
export async function uploadVideo(page: Page, filePath: string): Promise<void> {
  const input = page.locator('input[type="file"]').first();
  await input.setInputFiles(filePath);
  // The upload streams in chunks; wait for the completion toast before asserting.
  await expect(page.getByText(/upload complete|being inspected/i).first()).toBeVisible({ timeout: 180_000 });
}

/**
 * Authenticated REST access from a browser context.
 *
 * `page.request` shares the browser's cookie jar, so the session cookie travels
 * automatically. The CSRF token is *also* in a readable cookie (that is the whole
 * point of double-submit), so it is read from there and echoed as a header —
 * exactly what the frontend does.
 */
export class Api {
  constructor(private readonly page: Page) {}

  private async csrf(): Promise<string> {
    const cookies = await this.page.context().cookies();
    return cookies.find((cookie) => cookie.name === 'clipforge_csrf')?.value ?? '';
  }

  private async headers(): Promise<Record<string, string>> {
    return { 'x-csrf-token': await this.csrf(), 'content-type': 'application/json' };
  }

  async get(path: string): Promise<APIResponse> {
    return this.page.request.get(`/api/v1${path}`);
  }

  async post(path: string, data?: unknown): Promise<APIResponse> {
    return this.page.request.post(`/api/v1${path}`, { data, headers: await this.headers() });
  }

  async patch(path: string, data?: unknown): Promise<APIResponse> {
    return this.page.request.patch(`/api/v1${path}`, { data, headers: await this.headers() });
  }

  async json<T>(response: APIResponse): Promise<T> {
    expect(response.ok(), `${response.url()} -> ${response.status()} ${await response.text()}`).toBeTruthy();
    return (await response.json()) as T;
  }
}

export function api(page: Page): Api {
  return new Api(page);
}

/**
 * Job types as the API stores them. These are the `JobType` enum values — plain
 * names like "probe" never match, and a wait built on one would hang until
 * timeout instead of failing fast.
 */
export const JOB = {
  probe: 'video.probe',
  transcribe: 'video.transcribe',
  framing: 'video.analyze_framing',
  discover: 'project.discover_clips',
  render: 'clip.render',
} as const;

export interface JobRow {
  id: string;
  type: string;
  status: string;
  progress: number;
  stage: string;
  error_code?: string;
  error_message?: string;
  result?: Record<string, unknown>;
}

/**
 * Terminal job states.
 *
 * Success is `succeeded` (not `completed` — that word belongs to `job.stage`
 * and to transcript status). Getting this wrong turns every wait into a hang.
 */
const JOB_DONE = 'succeeded';
const JOB_FAILED = 'failed';
const JOB_CANCELED = 'canceled';

/** Wait until the newest job of `type` for a project reaches a terminal state. */
export async function waitForJob(
  client: Api,
  projectId: string,
  type: string,
  timeoutMs = 10 * 60_000,
): Promise<JobRow> {
  const deadline = Date.now() + timeoutMs;
  let last: JobRow | undefined;
  while (Date.now() < deadline) {
    const response = await client.get(`/jobs?project_id=${projectId}&limit=50`);
    if (response.ok()) {
      const body = (await response.json()) as { items: JobRow[] };
      const match = body.items.find((job) => job.type === type);
      if (match) {
        last = match;
        if (match.status === JOB_DONE) return match;
        if (match.status === JOB_FAILED) {
          throw new Error(`Job ${type} failed (${match.error_code}): ${match.error_message}`);
        }
        if (match.status === JOB_CANCELED) {
          throw new Error(`Job ${type} was cancelled before it finished`);
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error(`Timed out waiting for a ${type} job; last seen: ${JSON.stringify(last)}`);
}

/** Wait until the project's transcript reaches the given status. */
export async function waitForTranscript(client: Api, videoId: string, timeoutMs = 10 * 60_000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await client.get(`/videos/${videoId}/transcript`);
    if (response.ok()) {
      const body = (await response.json()) as { id?: string; status?: string } | null;
      if (body?.status === 'completed' && body.id) return body.id;
      if (body?.status === 'failed') throw new Error('transcription failed');
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error('Timed out waiting for the transcript');
}

/**
 * Make transcription succeed regardless of what the deployment can run.
 *
 * With ASR weights available, plain transcription works. Without them (offline
 * builds, no GPU, blocked model downloads) the supported path is script
 * alignment: paste the narration the demo asset was built from, and DTW produces
 * real word-level timings. The spec checks the reported capabilities rather than
 * assuming which world it is in, and never fakes a transcript.
 */
export async function ensureTranscribable(client: Api, videoId: string): Promise<'provider' | 'script_alignment'> {
  const capabilities = await client.json<{
    transcription: { local: { available: boolean }; api: { available: boolean }; script_alignment?: { available: boolean } };
  }>(await client.get('/capabilities'));

  const providerAvailable = capabilities.transcription.local.available || capabilities.transcription.api.available;
  if (providerAvailable) return 'provider';

  const narration = readNarration();
  if (!narration.trim()) {
    throw new Error(
      'No transcription provider is available and demo/narration.txt is missing, so this run cannot transcribe.\n' +
        'Generate the demo asset first: python scripts/make_demo_asset.py --out demo',
    );
  }
  expect(
    capabilities.transcription.script_alignment?.available,
    'no transcription provider and script alignment is unavailable: there is no honest way to transcribe',
  ).toBeTruthy();

  await client.json(await client.patch(`/videos/${videoId}`, { narration_script: narration }));
  return 'script_alignment';
}
