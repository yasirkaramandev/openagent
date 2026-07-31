import { CalculatorButton } from './CalculatorButton';

export type CalculatorAction =
  | { type: 'digit'; value: string }
  | { type: 'decimal' }
  | { type: 'operator'; value: '+' | '-' | '*' | '/' | '^' }
  | { type: 'equals' }
  | { type: 'clear' }
  | { type: 'clearEntry' }
  | { type: 'backspace' }
  | { type: 'sign' }
  | { type: 'percent' };

interface KeypadProps {
  onAction: (action: CalculatorAction) => void;
  activeKey: string | null;
}

interface KeyDefinition {
  visual: string;
  label: string;
  action: CalculatorAction;
  id: string;
  tone?: 'number' | 'utility' | 'operator' | 'equals';
  shortcut?: string;
  wide?: boolean;
}

const keys: readonly KeyDefinition[] = [
  {
    visual: 'AC',
    label: 'Clear all',
    action: { type: 'clear' },
    id: 'clear',
    tone: 'utility',
    shortcut: 'Escape',
  },
  {
    visual: 'CE',
    label: 'Clear entry',
    action: { type: 'clearEntry' },
    id: 'clearEntry',
    tone: 'utility',
    shortcut: 'Delete',
  },
  {
    visual: '⌫',
    label: 'Backspace',
    action: { type: 'backspace' },
    id: 'backspace',
    tone: 'utility',
    shortcut: 'Backspace',
  },
  {
    visual: '÷',
    label: 'Divide',
    action: { type: 'operator', value: '/' },
    id: '/',
    tone: 'operator',
    shortcut: '/',
  },
  {
    visual: '7',
    label: 'Seven',
    action: { type: 'digit', value: '7' },
    id: '7',
  },
  {
    visual: '8',
    label: 'Eight',
    action: { type: 'digit', value: '8' },
    id: '8',
  },
  {
    visual: '9',
    label: 'Nine',
    action: { type: 'digit', value: '9' },
    id: '9',
  },
  {
    visual: '×',
    label: 'Multiply',
    action: { type: 'operator', value: '*' },
    id: '*',
    tone: 'operator',
    shortcut: '*',
  },
  {
    visual: '4',
    label: 'Four',
    action: { type: 'digit', value: '4' },
    id: '4',
  },
  {
    visual: '5',
    label: 'Five',
    action: { type: 'digit', value: '5' },
    id: '5',
  },
  {
    visual: '6',
    label: 'Six',
    action: { type: 'digit', value: '6' },
    id: '6',
  },
  {
    visual: '−',
    label: 'Subtract',
    action: { type: 'operator', value: '-' },
    id: '-',
    tone: 'operator',
    shortcut: '-',
  },
  {
    visual: '1',
    label: 'One',
    action: { type: 'digit', value: '1' },
    id: '1',
  },
  {
    visual: '2',
    label: 'Two',
    action: { type: 'digit', value: '2' },
    id: '2',
  },
  {
    visual: '3',
    label: 'Three',
    action: { type: 'digit', value: '3' },
    id: '3',
  },
  {
    visual: '+',
    label: 'Add',
    action: { type: 'operator', value: '+' },
    id: '+',
    tone: 'operator',
    shortcut: '+',
  },
  {
    visual: '+/−',
    label: 'Toggle positive or negative',
    action: { type: 'sign' },
    id: 'sign',
    tone: 'utility',
  },
  {
    visual: '0',
    label: 'Zero',
    action: { type: 'digit', value: '0' },
    id: '0',
  },
  {
    visual: '.',
    label: 'Decimal point',
    action: { type: 'decimal' },
    id: '.',
    shortcut: '.',
  },
  {
    visual: '%',
    label: 'Percent',
    action: { type: 'percent' },
    id: '%',
    tone: 'utility',
    shortcut: '%',
  },
  {
    visual: '=',
    label: 'Equals',
    action: { type: 'equals' },
    id: 'equals',
    tone: 'equals',
    shortcut: 'Enter',
    wide: true,
  },
];

export function Keypad({ onAction, activeKey }: KeypadProps) {
  return (
    <div className="keypad" aria-label="Calculator keypad">
      {keys.map((key) => (
        <CalculatorButton
          key={key.id}
          label={key.label}
          tone={key.tone}
          wide={key.wide}
          aria-keyshortcuts={key.shortcut}
          keyboardActive={activeKey === key.id}
          onClick={() => onAction(key.action)}
        >
          {key.visual}
        </CalculatorButton>
      ))}
    </div>
  );
}
