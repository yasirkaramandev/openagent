import type { HistoryEntry } from './history';

interface HistoryPanelProps {
  open: boolean;
  entries: HistoryEntry[];
  privateMode: boolean;
  onClose: () => void;
  onReuse: (entry: HistoryEntry) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
}

export function HistoryPanel({
  open,
  entries,
  privateMode,
  onClose,
  onReuse,
  onRemove,
  onClear,
}: HistoryPanelProps) {
  if (!open) return null;

  return (
    <>
      <button
        type="button"
        className="panel-backdrop"
        aria-label="Close history"
        onClick={onClose}
      />
      <aside
        className="side-panel side-panel--history"
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-title"
      >
        <header className="side-panel__header">
          <div>
            <span className="side-panel__eyebrow">Local calculations</span>
            <h2 id="history-title">History</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close history"
            onClick={onClose}
            autoFocus
          >
            ×
          </button>
        </header>

        {privateMode ? (
          <p className="panel-empty">
            Private mode is on. New calculations are not saved.
          </p>
        ) : entries.length === 0 ? (
          <p className="panel-empty">
            Completed calculations will appear here.
          </p>
        ) : (
          <>
            <ol className="history-list">
              {entries.map((entry) => (
                <li className="history-item" key={entry.id}>
                  <button
                    type="button"
                    className="history-item__reuse"
                    aria-label={`Reuse ${entry.expression}`}
                    onClick={() => onReuse(entry)}
                  >
                    <span>{entry.expression}</span>
                    <strong>= {entry.result}</strong>
                  </button>
                  <button
                    type="button"
                    className="history-item__remove"
                    aria-label={`Remove ${entry.expression} from history`}
                    onClick={() => onRemove(entry.id)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ol>
            <button
              type="button"
              className="panel-button panel-button--danger"
              onClick={onClear}
            >
              Clear all history
            </button>
          </>
        )}
      </aside>
    </>
  );
}
