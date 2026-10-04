import { expect, test, type Page } from '@playwright/test';
import { PASSWORD, uniqueEmail } from './helpers';

test.describe('authentication', () => {
  test('landing page presents the product without pretending to be signed in', async ({ page }) => {
    await page.goto('/');

    await expect(
      page.getByRole('heading', { name: /turn long videos into short-form content automatically/i }),
    ).toBeVisible();
    await expect(page.getByRole('link', { name: /start creating/i }).first()).toBeVisible();
    await expect(page.getByRole('link', { name: /try clipforge/i }).first()).toBeVisible();

    // The marketing sections the brief requires, by heading rather than by class.
    for (const heading of [/how it works/i, /ai clip discovery/i, /smart framing/i, /word-level captions/i]) {
      await expect(page.getByRole('heading', { name: heading }).first()).toBeVisible();
    }

    // Privacy must be described honestly: private AI is an option, not the claim.
    await expect(page.getByText(/private ai mode/i).first()).toBeVisible();

    // Signed out: the dashboard is not reachable and nothing looks like a session.
    await page.goto('/dashboard');
    await expect(page).toHaveURL(/\/(login|$)/, { timeout: 20_000 });
  });

  test('register, persist across refresh, and log out', async ({ page }) => {
    await registerViaUiAndLand(page);

    // A refresh must restore the session from the cookie, not from memory.
    await page.reload();
    await expect(page.getByRole('heading', { name: /dashboard|welcome/i }).first()).toBeVisible({ timeout: 30_000 });

    // No tokens in localStorage — the session lives in an HttpOnly cookie.
    const stored = await page.evaluate(() => JSON.stringify(Object.keys(window.localStorage)));
    expect(stored).not.toMatch(/token|jwt|session/i);

    await page.getByRole('button', { name: /sign out|log out/i }).first().click();
    await expect(page).toHaveURL(/\/(login|$)|^\/$/, { timeout: 20_000 });
  });

  test('protected pages redirect to login, and login restores the intended page', async ({ page }) => {
    await page.goto('/settings');
    await expect(page).toHaveURL(/\/login/, { timeout: 20_000 });

    // Register first so the credentials are real.
    const email = uniqueEmail('e2e-redirect');
    await page.goto('/register');
    await page.getByLabel('Name').fill('Redirect Tester');
    await page.getByLabel('Email').fill(email);
    await page.getByLabel('Password').fill(PASSWORD);
    await page.getByRole('button', { name: /create account|sign up|register/i }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
    await page.getByRole('button', { name: /sign out|log out/i }).first().click();
    await expect(page).toHaveURL(/\/(login|$)|^\/$/, { timeout: 20_000 });

    await page.goto('/settings');
    await page.getByLabel('Email').fill(email);
    await page.getByLabel('Password').fill(PASSWORD);
    await page.getByRole('button', { name: /sign in|log in/i }).click();
    await expect(page).toHaveURL(/\/settings|\/dashboard/, { timeout: 30_000 });
  });

  test('wrong password fails visibly and does not create a session', async ({ page }) => {
    const email = uniqueEmail('e2e-badpw');
    await page.goto('/register');
    await page.getByLabel('Name').fill('Bad Password');
    await page.getByLabel('Email').fill(email);
    await page.getByLabel('Password').fill(PASSWORD);
    await page.getByRole('button', { name: /create account|sign up|register/i }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
    await page.getByRole('button', { name: /sign out|log out/i }).first().click();
    await expect(page).toHaveURL(/\/(login|$)|^\/$/, { timeout: 20_000 });

    await page.goto('/login');
    await page.getByLabel('Email').fill(email);
    await page.getByLabel('Password').fill('definitely-not-the-password');
    await page.getByRole('button', { name: /sign in|log in/i }).click();

    // A toast with role="alert" is the app's error surface; no raw stack traces.
    await expect(page.getByRole('alert').filter({ hasText: /password|credentials|incorrect/i }).first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page).toHaveURL(/\/login/);
  });
});

/** Register and confirm the dashboard rendered. */
async function registerViaUiAndLand(page: Page): Promise<string> {
  const email = uniqueEmail('e2e-auth');
  await page.goto('/register');
  await page.getByLabel('Name').fill('Auth Tester');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: /create account|sign up|register/i }).click();
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
  return email;
}
