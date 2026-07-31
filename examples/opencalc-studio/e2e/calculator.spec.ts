import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
});

test('performs a standard calculation', async ({ page }) => {
  await page.getByRole('button', { name: 'Seven' }).click();
  await page.getByRole('button', { name: 'Multiply' }).click();
  await page.getByRole('button', { name: 'Eight' }).click();
  await page.getByRole('button', { name: 'Equals' }).click();

  await expect(page.getByLabel('Result: 56')).toHaveText('56');
  await expect(page.getByLabel('Expression: 7 × 8 =')).toBeVisible();
});

test('evaluates a scientific function', async ({ page }) => {
  await page.getByRole('button', { name: 'SCI' }).click();
  await page.getByRole('button', { name: 'Three' }).click();
  await page.getByRole('button', { name: 'Zero' }).click();
  await page.getByRole('button', { name: 'Sine', exact: true }).click();

  await expect(page.getByLabel('Result: 0.5')).toHaveText('0.5');
  await expect(page.getByLabel('Scientific functions')).toBeVisible();

  await page.getByRole('button', { name: 'RAD' }).click();
  await expect(page.getByLabel('Result: −0.9880316241')).toHaveText(
    '−0.9880316241',
  );
});

test('supports keyboard-only entry and Enter', async ({ page }) => {
  await page.locator('body').pressSequentially('12*3', { delay: 50 });
  await page.keyboard.press('Enter');

  await expect(page.getByLabel('Result: 36')).toHaveText('36');
  await expect(page.getByLabel('Expression: 12 × 3 =')).toBeVisible();
});

test('reuses a calculation from history', async ({ page }) => {
  await page.locator('body').pressSequentially('2+3', { delay: 50 });
  await page.keyboard.press('Enter');
  await page.getByRole('button', { name: 'Open history, 1 entries' }).click();
  await page.getByRole('button', { name: 'Reuse 2 + 3' }).click();

  await expect(page.getByLabel('Expression: 2 + 3')).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.getByLabel('Result: 5')).toHaveText('5');
});

test('fits and remains usable at a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.reload();

  await expect(
    page.getByRole('region', { name: 'Standard calculator' }),
  ).toBeVisible();
  await page.getByRole('button', { name: 'Nine' }).click();
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  await page.getByRole('button', { name: 'One' }).click();
  await page.getByRole('button', { name: 'Equals' }).click();
  await expect(page.getByLabel('Result: 10')).toHaveText('10');

  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(hasHorizontalOverflow).toBe(false);
});
