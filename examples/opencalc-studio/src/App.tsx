import { Calculator } from './ui/Calculator';
import { PwaUpdatePrompt } from './ui/PwaUpdatePrompt';
import { ThemeControl } from './ui/ThemeControl';
import { useCalculatorSettings } from './ui/settings';
import { useTheme } from './ui/useTheme';

export default function App() {
  const { settings, updateSettings } = useCalculatorSettings();
  useTheme(settings.theme);

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="brand" href="/" aria-label="OpenCalc Studio home">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
            <span />
          </span>
          <span>
            <strong>OpenCalc</strong>
            <small>Studio</small>
          </span>
        </a>

        <ThemeControl
          theme={settings.theme}
          onChange={(theme) => updateSettings({ theme })}
        />
      </header>

      <main className="calculator-stage">
        <Calculator settings={settings} onSettingsChange={updateSettings} />
      </main>

      <footer className="app-footer">
        <span>Standard + scientific</span>
        <span aria-hidden="true">•</span>
        <span>Keyboard ready</span>
      </footer>

      <PwaUpdatePrompt />
    </div>
  );
}
