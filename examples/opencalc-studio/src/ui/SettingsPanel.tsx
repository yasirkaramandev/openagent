import type { CalculatorSettings, ThemePreference } from './settings';

interface SettingsPanelProps {
  open: boolean;
  settings: CalculatorSettings;
  onClose: () => void;
  onChange: (updates: Partial<CalculatorSettings>) => void;
}

const themes: ReadonlyArray<{ value: ThemePreference; label: string }> = [
  { value: 'light', label: 'Light' },
  { value: 'system', label: 'System' },
  { value: 'dark', label: 'Dark' },
];

export function SettingsPanel({
  open,
  settings,
  onClose,
  onChange,
}: SettingsPanelProps) {
  if (!open) return null;

  return (
    <>
      <button
        type="button"
        className="panel-backdrop"
        aria-label="Close settings"
        onClick={onClose}
      />
      <aside
        className="side-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <header className="side-panel__header">
          <div>
            <span className="side-panel__eyebrow">Display & privacy</span>
            <h2 id="settings-title">Settings</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close settings"
            onClick={onClose}
            autoFocus
          >
            ×
          </button>
        </header>

        <div className="settings-list">
          <label className="setting-row">
            <span>
              <strong>Digit grouping</strong>
              <small>Show thousands separators</small>
            </span>
            <input
              type="checkbox"
              checked={settings.digitGrouping}
              onChange={(event) =>
                onChange({ digitGrouping: event.currentTarget.checked })
              }
            />
          </label>

          <label className="setting-field">
            <span>Decimal places</span>
            <select
              value={settings.decimalPlaces}
              onChange={(event) =>
                onChange({ decimalPlaces: Number(event.currentTarget.value) })
              }
            >
              {Array.from({ length: 13 }, (_, value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <fieldset className="setting-group">
            <legend>Angle mode</legend>
            <div className="setting-segmented">
              {(['DEG', 'RAD'] as const).map((mode) => (
                <button
                  type="button"
                  key={mode}
                  aria-pressed={settings.angleMode === mode}
                  onClick={() => onChange({ angleMode: mode })}
                >
                  {mode}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="setting-group">
            <legend>Theme</legend>
            <div className="setting-segmented setting-segmented--three">
              {themes.map((theme) => (
                <button
                  type="button"
                  key={theme.value}
                  aria-pressed={settings.theme === theme.value}
                  onClick={() => onChange({ theme: theme.value })}
                >
                  {theme.label}
                </button>
              ))}
            </div>
          </fieldset>

          <label className="setting-row setting-row--private">
            <span>
              <strong>Private mode</strong>
              <small>Do not save calculation history</small>
            </span>
            <input
              type="checkbox"
              checked={settings.privateMode}
              onChange={(event) =>
                onChange({ privateMode: event.currentTarget.checked })
              }
            />
          </label>
        </div>
      </aside>
    </>
  );
}
