import type { ButtonHTMLAttributes, ReactNode } from 'react';

type ButtonTone = 'number' | 'utility' | 'operator' | 'equals';

interface CalculatorButtonProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'children'
> {
  label: string;
  children: ReactNode;
  tone?: ButtonTone;
  wide?: boolean;
  keyboardActive?: boolean;
}

export function CalculatorButton({
  label,
  children,
  tone = 'number',
  wide = false,
  keyboardActive = false,
  className = '',
  ...buttonProps
}: CalculatorButtonProps) {
  const classes = [
    'calc-button',
    `calc-button--${tone}`,
    wide ? 'calc-button--wide' : '',
    keyboardActive ? 'is-keyboard-active' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <button
      type="button"
      className={classes}
      aria-label={label}
      {...buttonProps}
    >
      {children}
    </button>
  );
}
