import { useCallback, useEffect, useRef, useState } from 'react';

export interface HistoryEntry {
  id: string;
  expression: string;
  result: string;
  createdAt: number;
}

interface StoredHistory {
  version: 1;
  entries: HistoryEntry[];
}

const STORAGE_KEY = 'opencalc.history';
const HISTORY_VERSION = 1;
const MAX_HISTORY_ENTRIES = 50;

function storageAvailable() {
  return typeof window !== 'undefined' && 'localStorage' in window;
}

function isHistoryEntry(value: unknown): value is HistoryEntry {
  if (!value || typeof value !== 'object') return false;
  const entry = value as Partial<HistoryEntry>;
  return (
    typeof entry.id === 'string' &&
    typeof entry.expression === 'string' &&
    typeof entry.result === 'string' &&
    typeof entry.createdAt === 'number' &&
    Number.isFinite(entry.createdAt)
  );
}

function readHistory(): HistoryEntry[] {
  if (!storageAvailable()) return [];

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return [];
    const stored = JSON.parse(raw) as Partial<StoredHistory>;
    if (
      stored.version !== HISTORY_VERSION ||
      !Array.isArray(stored.entries) ||
      !stored.entries.every(isHistoryEntry)
    ) {
      window.localStorage.removeItem(STORAGE_KEY);
      return [];
    }
    return stored.entries.slice(0, MAX_HISTORY_ENTRIES);
  } catch {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Storage can be unavailable even when the API is present.
    }
    return [];
  }
}

export function useCalculatorHistory(privateMode: boolean) {
  const [entries, setEntries] = useState<HistoryEntry[]>(() =>
    privateMode ? [] : readHistory(),
  );
  const sequence = useRef(0);

  useEffect(() => {
    if (!storageAvailable()) return;
    try {
      if (privateMode) {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        const stored: StoredHistory = {
          version: HISTORY_VERSION,
          entries,
        };
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
      }
    } catch {
      // History remains usable in memory when storage is unavailable.
    }
  }, [entries, privateMode]);

  const addEntry = useCallback(
    (expression: string, result: string) => {
      if (privateMode) return;
      const entry: HistoryEntry = {
        id: `${Date.now()}-${sequence.current++}`,
        expression,
        result,
        createdAt: Date.now(),
      };
      setEntries((current) =>
        [entry, ...current].slice(0, MAX_HISTORY_ENTRIES),
      );
    },
    [privateMode],
  );

  const removeEntry = useCallback((id: string) => {
    setEntries((current) => current.filter((entry) => entry.id !== id));
  }, []);

  const clearEntries = useCallback(() => setEntries([]), []);

  return { entries, addEntry, removeEntry, clearEntries };
}
