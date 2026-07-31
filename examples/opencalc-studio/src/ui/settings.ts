import { useCallback, useEffect, useState } from 'react';
import type { AngleMode } from '../calculator';

export type ThemePreference = 'light' | 'system' | 'dark';

export interface CalculatorSettings {
  digitGrouping: boolean;
  decimalPlaces: number;
  angleMode: AngleMode;
  theme: ThemePreference;
  privateMode: boolean;
}

interface StoredSettings {
  version: 1;
  settings: CalculatorSettings;
}

const STORAGE_KEY = 'opencalc.settings';
const SETTINGS_VERSION = 1;

export const DEFAULT_SETTINGS: CalculatorSettings = {
  digitGrouping: true,
  decimalPlaces: 10,
  angleMode: 'DEG',
  theme: 'system',
  privateMode: false,
};

function storageAvailable() {
  return typeof window !== 'undefined' && 'localStorage' in window;
}

function isTheme(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'system' || value === 'dark';
}

function isAngleMode(value: unknown): value is AngleMode {
  return value === 'DEG' || value === 'RAD';
}

function isSettings(value: unknown): value is CalculatorSettings {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<CalculatorSettings>;
  return (
    typeof candidate.digitGrouping === 'boolean' &&
    Number.isInteger(candidate.decimalPlaces) &&
    (candidate.decimalPlaces ?? -1) >= 0 &&
    (candidate.decimalPlaces ?? 13) <= 12 &&
    isAngleMode(candidate.angleMode) &&
    isTheme(candidate.theme) &&
    typeof candidate.privateMode === 'boolean'
  );
}

function readSettings(): CalculatorSettings {
  if (!storageAvailable()) return DEFAULT_SETTINGS;

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_SETTINGS;

    const stored = JSON.parse(raw) as Partial<StoredSettings>;
    if (stored.version !== SETTINGS_VERSION || !isSettings(stored.settings)) {
      window.localStorage.removeItem(STORAGE_KEY);
      return DEFAULT_SETTINGS;
    }
    return stored.settings;
  } catch {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Storage can be unavailable even when the API is present.
    }
    return DEFAULT_SETTINGS;
  }
}

export function useCalculatorSettings() {
  const [settings, setSettings] = useState<CalculatorSettings>(readSettings);

  useEffect(() => {
    if (!storageAvailable()) return;
    const stored: StoredSettings = { version: SETTINGS_VERSION, settings };
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    } catch {
      // Preferences remain usable for the current session.
    }
  }, [settings]);

  const updateSettings = useCallback((updates: Partial<CalculatorSettings>) => {
    setSettings((current) => ({ ...current, ...updates }));
  }, []);

  return { settings, updateSettings };
}
