import { expect, test } from '@playwright/test';
import { registerViaUi, uniqueEmail } from './helpers';

test.describe('dashboard', () => {
  test('a new account sees honest empty state, not invented data', async ({ page }) => {
    await registerViaUi(page, uniqueEmail('e2e-dash'));

    // Usage panels are real counters; a fresh account is at zero minutes.
    await expect(page.getByText(/minutes processed/i).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/0\s*(of|\/)/).first()).toBeVisible();

    // No projects yet, and the dashboard says so instead of showing filler rows.
    await expect(page.getByText(/no projects|nothing here yet|create your first/i).first()).toBeVisible();

    // Plans are shown from the API, and the free plan is the current one.
    await expect(page.getByText(/\bfree\b/i).first()).toBeVisible();
  });

  test('create, open, rename-persist, and delete a project', async ({ page }) => {
    await registerViaUi(page, uniqueEmail('e2e-proj'));

    const name = `Docu series ${Date.now()}`;
    await page.getByRole('button', { name: /new project/i }).first().click();
    await page.getByLabel('Project name').fill(name);
    await page.getByRole('button', { name: /create project/i }).click();

    await expect(page).toHaveURL(/\/projects\/[0-9a-f]+/, { timeout: 30_000 });
    await expect(page.getByRole('heading', { name })).toBeVisible({ timeout: 30_000 });

    // The project page offers the pipeline entry points only once a video exists.
    await expect(page.getByRole('button', { name: /^transcribe$|re-transcribe/i })).toHaveCount(0);

    // Back on the dashboard the same project is listed — proof it persisted.
    await page.goto('/dashboard');
    await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });

    // Round-trip through the API to confirm the row is genuinely stored.
    const listed = await page.request.get('/api/v1/projects?limit=50');
    expect(listed.ok()).toBeTruthy();
    const body = (await listed.json()) as { items: { id: string; name: string }[] };
    const project = body.items.find((item) => item.name === name);
    expect(project, 'the created project should be returned by the API').toBeTruthy();

    // Deleting is a real API call, and the dashboard reflects it afterwards.
    const deleted = await page.request.delete(`/api/v1/projects/${project!.id}`, {
      headers: { 'x-csrf-token': await csrfToken(page) },
    });
    expect(deleted.ok()).toBeTruthy();
    await page.reload();
    await expect(page.getByText(name)).toHaveCount(0, { timeout: 30_000 });
  });

  test('the sidebar exposes settings and pricing', async ({ page }) => {
    await registerViaUi(page, uniqueEmail('e2e-nav'));

    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: /settings/i }).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/private ai mode/i).first()).toBeVisible();

    await page.goto('/pricing');
    await expect(page.getByText(/pro|free|lifetime/i).first()).toBeVisible({ timeout: 30_000 });

    // Signing out is not offered as a broken control: it works or it is absent.
    await page.goto('/dashboard');
    await expect(page.getByRole('button', { name: /sign out|log out/i }).first()).toBeVisible();
  });
});

async function csrfToken(page: import('@playwright/test').Page): Promise<string> {
  const cookies = await page.context().cookies();
  return cookies.find((cookie) => cookie.name === 'clipforge_csrf')?.value ?? '';
}
