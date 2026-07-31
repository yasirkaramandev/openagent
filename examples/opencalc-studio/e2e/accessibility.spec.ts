import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('main view has no serious or critical accessibility violations', async ({
  page,
}) => {
  await page.goto('/');

  const scan = await new AxeBuilder({ page }).analyze();
  const seriousOrCritical = scan.violations.filter(
    ({ impact }) => impact === 'serious' || impact === 'critical',
  );

  expect(seriousOrCritical).toEqual([]);

  const settingsButton = page.getByRole('button', { name: 'Open settings' });
  await settingsButton.click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await expect(
    dialog.getByRole('button', { name: 'Close settings' }),
  ).toBeFocused();

  await page.keyboard.press('Shift+Tab');
  await expect
    .poll(() =>
      page.evaluate(() =>
        Boolean(
          document
            .querySelector('[role="dialog"]')
            ?.contains(document.activeElement),
        ),
      ),
    )
    .toBe(true);

  await page.keyboard.press('Escape');
  await expect(settingsButton).toBeFocused();
});
