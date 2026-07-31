import { CalculatorButton } from './CalculatorButton';

export type ScientificAction =
  | {
      type: 'function';
      name:
        | 'sin'
        | 'cos'
        | 'tan'
        | 'asin'
        | 'acos'
        | 'atan'
        | 'ln'
        | 'log10'
        | 'sqrt';
    }
  | { type: 'square' }
  | { type: 'power' }
  | { type: 'reciprocal' }
  | { type: 'factorial' }
  | { type: 'constant'; name: 'pi' | 'e' };

interface ScientificKeypadProps {
  onAction: (action: ScientificAction) => void;
}

const keys: ReadonlyArray<{
  visual: string;
  label: string;
  action: ScientificAction;
}> = [
  { visual: 'sin', label: 'Sine', action: { type: 'function', name: 'sin' } },
  { visual: 'cos', label: 'Cosine', action: { type: 'function', name: 'cos' } },
  {
    visual: 'tan',
    label: 'Tangent',
    action: { type: 'function', name: 'tan' },
  },
  {
    visual: 'sin⁻¹',
    label: 'Inverse sine',
    action: { type: 'function', name: 'asin' },
  },
  {
    visual: 'cos⁻¹',
    label: 'Inverse cosine',
    action: { type: 'function', name: 'acos' },
  },
  {
    visual: 'tan⁻¹',
    label: 'Inverse tangent',
    action: { type: 'function', name: 'atan' },
  },
  {
    visual: 'ln',
    label: 'Natural logarithm',
    action: { type: 'function', name: 'ln' },
  },
  {
    visual: 'log',
    label: 'Base ten logarithm',
    action: { type: 'function', name: 'log10' },
  },
  {
    visual: '√x',
    label: 'Square root',
    action: { type: 'function', name: 'sqrt' },
  },
  { visual: 'x²', label: 'Square', action: { type: 'square' } },
  { visual: 'xʸ', label: 'Raise to a power', action: { type: 'power' } },
  { visual: '1/x', label: 'Reciprocal', action: { type: 'reciprocal' } },
  { visual: 'x!', label: 'Factorial', action: { type: 'factorial' } },
  { visual: 'π', label: 'Pi', action: { type: 'constant', name: 'pi' } },
  {
    visual: 'e',
    label: 'Euler’s number',
    action: { type: 'constant', name: 'e' },
  },
];

export function ScientificKeypad({ onAction }: ScientificKeypadProps) {
  return (
    <div className="scientific-keypad" aria-label="Scientific functions">
      {keys.map((key) => (
        <CalculatorButton
          key={key.label}
          label={key.label}
          tone="utility"
          className="calc-button--scientific"
          onClick={() => onAction(key.action)}
        >
          {key.visual}
        </CalculatorButton>
      ))}
    </div>
  );
}
