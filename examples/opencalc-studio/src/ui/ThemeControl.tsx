import type { ThemePreference } from './settings';

interface ThemeControlProps {
  theme: ThemePreference;
  onChange: (theme: ThemePreference) => void;
}

const options: ReadonlyArray<{
  value: ThemePreference;
  label: string;
  symbol: string;
}> = [
  { value: 'light', label: 'Use light theme', symbol: '☼' },
  { value: 'system', label: 'Follow system theme', symbol: '◐' },
  { value: 'dark', label: 'Use dark theme', symbol: '☾' },
];

export function ThemeControl({ theme, onChange }: ThemeControlProps) {
  return (
    <div className="theme-control" role="group" aria-label="Color theme">
      {options.map((option) => (
        <button
          className="theme-option"
          type="button"
          key={option.value}
          aria-label={option.label}
          aria-pressed={theme === option.value}
          title={option.value[0]!.toUpperCase() + option.value.slice(1)}
          onClick={() => onChange(option.value)}
        >
          <span aria-hidden="true">{option.symbol}</span>
        </button>
      ))}
    </div>
  );
}
