import { useEffect } from 'react';
import type { ThemePreference } from './settings';

export function useTheme(theme: ThemePreference) {
  useEffect(() => {
    const root = document.documentElement;

    if (theme === 'system') {
      root.removeAttribute('data-theme');
    } else {
      root.dataset.theme = theme;
    }
  }, [theme]);
}
