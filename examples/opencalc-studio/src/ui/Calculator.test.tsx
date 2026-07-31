import { useState } from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import App from '../App';
import { Calculator } from './Calculator';
import { DEFAULT_SETTINGS, type CalculatorSettings } from './settings';

function CalculatorHarness() {
  const [settings, setSettings] =
    useState<CalculatorSettings>(DEFAULT_SETTINGS);

  return (
    <Calculator
      settings={settings}
      onSettingsChange={(updates) =>
        setSettings((current) => ({ ...current, ...updates }))
      }
    />
  );
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('style');
});

describe('Calculator UI', () => {
  it('computes keypad and safely tokenized paste expressions', async () => {
    const user = userEvent.setup();
    render(<CalculatorHarness />);

    await user.click(screen.getByRole('button', { name: 'Seven' }));
    await user.click(screen.getByRole('button', { name: 'Multiply' }));
    await user.click(screen.getByRole('button', { name: 'Eight' }));
    await user.click(screen.getByRole('button', { name: 'Equals' }));

    expect(screen.getByLabelText('Result: 56').textContent).toBe('56');

    await user.click(screen.getByRole('button', { name: 'Equals' }));
    expect(screen.getByLabelText('Result: 448').textContent).toBe('448');

    const safePaste = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(safePaste, 'clipboardData', {
      value: { getData: () => ' (12 + 3) * 2 ' },
    });
    document.body.dispatchEvent(safePaste);

    await waitFor(() => {
      expect(screen.getByLabelText('Result: 30').textContent).toBe('30');
    });

    const unsafePaste = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(unsafePaste, 'clipboardData', {
      value: { getData: () => 'alert(1)' },
    });
    document.body.dispatchEvent(unsafePaste);

    await waitFor(() => {
      expect(
        screen.getByLabelText(
          'Error: Paste numbers with +, −, ×, ÷, ^, parentheses, or ! only',
        ),
      ).toBeTruthy();
    });

    const ambiguousPaste = new Event('paste', {
      bubbles: true,
      cancelable: true,
    });
    Object.defineProperty(ambiguousPaste, 'clipboardData', {
      value: { getData: () => '10%3' },
    });
    document.body.dispatchEvent(ambiguousPaste);

    await waitFor(() => {
      expect(
        screen.getByLabelText(
          'Error: Paste numbers with +, −, ×, ÷, ^, parentheses, or ! only',
        ),
      ).toBeTruthy();
    });

    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    await user.click(screen.getByRole('button', { name: 'Two' }));
    await user.click(screen.getByRole('button', { name: 'Zero' }));
    await user.click(screen.getByRole('button', { name: 'Zero' }));
    await user.click(screen.getByRole('button', { name: /^Add$/ }));
    await user.click(screen.getByRole('button', { name: 'One' }));
    await user.click(screen.getByRole('button', { name: 'Zero' }));
    await user.click(screen.getByRole('button', { name: 'Percent' }));
    await user.click(screen.getByRole('button', { name: 'Equals' }));
    expect(screen.getByLabelText('Result: 220').textContent).toBe('220');

    await user.click(
      screen.getByRole('button', { name: 'Toggle positive or negative' }),
    );
    await user.click(screen.getByRole('button', { name: 'Equals' }));
    expect(screen.getByLabelText('Result: −220').textContent).toBe('−220');

    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    await user.click(screen.getByRole('button', { name: 'One' }));
    await user.click(screen.getByRole('button', { name: 'Divide' }));
    await user.click(screen.getByRole('button', { name: 'Zero' }));
    await user.click(screen.getByRole('button', { name: 'Equals' }));
    expect(
      screen.getByLabelText('Error: Cannot divide by zero').textContent,
    ).toBe('Cannot divide by zero');
  });

  it('reveals the scientific keys when scientific mode is selected', async () => {
    const user = userEvent.setup();
    render(<CalculatorHarness />);

    expect(screen.queryByLabelText('Scientific functions')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'SCI' }));

    expect(screen.getByLabelText('Scientific functions')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sine' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Square root' })).toBeTruthy();
  });

  it('adds the display to memory and recalls it with MR', async () => {
    const user = userEvent.setup();
    render(<CalculatorHarness />);

    await user.click(screen.getByRole('button', { name: 'Seven' }));
    await user.click(
      screen.getByRole('button', { name: 'Add display to memory' }),
    );
    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    await user.click(screen.getByRole('button', { name: 'Recall memory' }));

    expect(screen.getByLabelText('Result: 7').textContent).toBe('7');
    expect(screen.getByLabelText('Memory contains a value')).toBeTruthy();
  });

  it('records a completed calculation in history', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem('opencalc.history', '{malformed');
    render(<CalculatorHarness />);

    await user.click(screen.getByRole('button', { name: 'Two' }));
    await user.click(screen.getByRole('button', { name: 'Add' }));
    await user.click(screen.getByRole('button', { name: 'Three' }));
    await user.click(screen.getByRole('button', { name: 'Equals' }));
    await user.click(
      screen.getByRole('button', { name: 'Open history, 1 entries' }),
    );

    expect(screen.getByRole('dialog', { name: 'History' })).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'Reuse 2 + 3' }).textContent,
    ).toContain('= 5');

    await user.click(
      screen.getAllByRole('button', { name: 'Close history' })[0]!,
    );
    await user.click(screen.getByRole('button', { name: 'Open settings' }));
    await user.click(screen.getByRole('checkbox', { name: /Private mode/ }));

    await waitFor(() => {
      expect(window.localStorage.getItem('opencalc.history')).toBeNull();
    });

    await user.click(
      screen.getAllByRole('button', { name: 'Close settings' })[0]!,
    );
    await user.click(screen.getByRole('button', { name: 'One' }));
    await user.click(screen.getByRole('button', { name: /^Add$/ }));
    await user.click(screen.getByRole('button', { name: 'One' }));
    await user.click(screen.getByRole('button', { name: 'Equals' }));

    expect(
      screen.getByRole('button', { name: 'Open history, 0 entries' }),
    ).toBeTruthy();
    expect(window.localStorage.getItem('opencalc.history')).toBeNull();
  });

  it('persists settings and restores them on a new mount', async () => {
    const user = userEvent.setup();
    const firstRender = render(<App />);

    await user.click(screen.getByRole('button', { name: 'Open settings' }));
    const grouping = screen.getByRole('checkbox', {
      name: /Digit grouping/,
    }) as HTMLInputElement;
    expect(grouping.checked).toBe(true);
    await user.click(grouping);

    await waitFor(() => {
      const stored = JSON.parse(
        window.localStorage.getItem('opencalc.settings') ?? '{}',
      ) as { settings?: { digitGrouping?: boolean } };
      expect(stored.settings?.digitGrouping).toBe(false);
    });

    firstRender.unmount();
    const secondRender = render(<App />);
    await user.click(screen.getByRole('button', { name: 'Open settings' }));

    const restoredGrouping = screen.getByRole('checkbox', {
      name: /Digit grouping/,
    }) as HTMLInputElement;
    expect(restoredGrouping.checked).toBe(false);

    secondRender.unmount();
    window.localStorage.setItem(
      'opencalc.settings',
      JSON.stringify({ version: 99, settings: {} }),
    );
    render(<App />);
    await user.click(screen.getByRole('button', { name: 'Open settings' }));

    const recoveredGrouping = screen.getByRole('checkbox', {
      name: /Digit grouping/,
    }) as HTMLInputElement;
    expect(recoveredGrouping.checked).toBe(true);
  });
});
