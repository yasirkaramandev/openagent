import { useEffect, useState } from 'react';
import {
  dismissPwaUpdate,
  subscribeToPwaUpdate,
  type ApplyPwaUpdate,
} from '../pwa';

export function PwaUpdatePrompt() {
  const [applyUpdate, setApplyUpdate] = useState<ApplyPwaUpdate | null>(null);

  useEffect(
    () =>
      subscribeToPwaUpdate((update) => {
        setApplyUpdate(() => update);
      }),
    [],
  );

  if (!applyUpdate) {
    return null;
  }

  return (
    <aside
      className="pwa-update"
      role="status"
      aria-live="polite"
      aria-label="App update available"
    >
      <span>A fresh version is ready.</span>
      <button
        className="pwa-update__reload"
        type="button"
        onClick={() => void applyUpdate()}
      >
        Reload
      </button>
      <button
        className="pwa-update__dismiss"
        type="button"
        aria-label="Dismiss update notification"
        onClick={() => {
          dismissPwaUpdate();
          setApplyUpdate(null);
        }}
      >
        ×
      </button>
    </aside>
  );
}
