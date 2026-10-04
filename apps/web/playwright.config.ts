import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

/**
 * End-to-end configuration for the full ClipForge workflow.
 *
 * Both servers are started here so the suite is self-contained:
 *   - the API on 8000 against a *scratch* database and storage root, so a run
 *     never touches the developer's `.data/`
 *   - Vite on 5173, which proxies /api and /ws to the API (single origin, so the
 *     HttpOnly session cookie behaves exactly as it does in production)
 *
 * Set PLAYWRIGHT_REUSE_SERVERS=1 to attach to servers you already have running.
 */
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const API_PORT = Number(process.env.E2E_API_PORT ?? 8000);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5173);
const BASE_URL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${WEB_PORT}`;
const reuseExisting = process.env.PLAYWRIGHT_REUSE_SERVERS === '1';

/** Environment for the API process, isolated from local development state. */
const API_ENV = {
  ENVIRONMENT: 'test',
  DATABASE_URL: `sqlite:///${REPO_ROOT}/.data/e2e.db`,
  STORAGE_BACKEND: 'local',
  STORAGE_LOCAL_ROOT: `${REPO_ROOT}/.data/e2e-storage`,
  QUEUE_BACKEND: 'memory',
  WORKER_INLINE: 'true',
  // Job throughput matters more than realism while waiting on renders.
  WORKER_CONCURRENCY: '4',
  RATE_LIMIT_ENABLED: 'false',
  SEED_DEMO_DATA: 'false',
  LOG_LEVEL: 'WARNING',
  LOG_JSON: 'false',
  PROCESSOR_ENABLED: 'false',
  // The vision detector is exercised for real; `auto` resolves to what is installed.
  VISION_ENABLED: 'true',
  VISION_FACE_DETECTOR: 'auto',
  // Transcription: use a pasted script rather than ASR weights when provided.
  AI_TRANSCRIPTION_PROVIDER: process.env.AI_TRANSCRIPTION_PROVIDER ?? 'auto',
  AI_LLM_PROVIDER: process.env.AI_LLM_PROVIDER ?? 'none',
  CORS_ORIGINS: `${BASE_URL},http://localhost:${WEB_PORT}`,
  FRONTEND_URL: BASE_URL,
  SECRET_KEY: 'e2e-secret-key-not-for-production-use',
  JWT_SECRET: 'e2e-jwt-secret-not-for-production-use',
};

export default defineConfig({
  testDir: './e2e',
  // Media work is slow: transcription plus a render can exceed a minute.
  timeout: 10 * 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list'], ['html', { open: 'never' }]],
  outputDir: './e2e/test-results',

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },

  projects: [
    { name: 'chromium-desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    // The dashboard and review screens must work on a phone; the editor is
    // desktop/tablet-first by design, so it is not asserted at this width.
    { name: 'chromium-mobile', use: { ...devices['Pixel 7'] }, testMatch: /(auth|dashboard)\.spec\.ts/ },
  ],

  webServer: reuseExisting
    ? undefined
    : [
        {
          command: `./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT} --app-dir apps/api`,
          cwd: REPO_ROOT,
          env: API_ENV,
          url: `http://127.0.0.1:${API_PORT}/healthz`,
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
          stdout: 'pipe',
          stderr: 'pipe',
        },
        {
          command: `npx vite --host 127.0.0.1 --port ${WEB_PORT}`,
          cwd: __dirname,
          env: { VITE_DEV_API_TARGET: `http://127.0.0.1:${API_PORT}` },
          url: BASE_URL,
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
        },
      ],
});
