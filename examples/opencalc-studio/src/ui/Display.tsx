import type { AngleMode } from '../calculator';

interface DisplayProps {
  expression: string;
  result: string;
  isError: boolean;
  mode: 'STD' | 'SCI';
  angleMode: AngleMode;
  hasMemory: boolean;
}

export function Display({
  expression,
  result,
  isError,
  mode,
  angleMode,
  hasMemory,
}: DisplayProps) {
  return (
    <div className="display" aria-label="Calculator display">
      <div className="display__status">
        <span className="display__mode">
          {mode}
          {mode === 'SCI' ? ` / ${angleMode}` : ''}
          {hasMemory ? (
            <span
              className="display__memory"
              aria-label="Memory contains a value"
            >
              M
            </span>
          ) : null}
        </span>
        <span className="display__indicator" aria-label="Calculator is ready">
          READY
        </span>
      </div>

      <div
        className="display__expression"
        role="status"
        aria-label={expression ? `Expression: ${expression}` : 'No expression'}
      >
        {expression || <span aria-hidden="true">0</span>}
      </div>

      <output
        className={`display__result${isError ? ' display__result--error' : ''}`}
        aria-live="polite"
        aria-atomic="true"
        aria-label={isError ? `Error: ${result}` : `Result: ${result}`}
      >
        {result}
      </output>
    </div>
  );
}
