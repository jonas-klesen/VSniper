import { type InputHTMLAttributes, useEffect, useState } from 'react';

/**
 * Numeric text input that keeps the raw keystrokes locally and only normalizes on blur — so
 * intermediate values like "5." or a leading "0" while typing "0.99" aren't erased mid-keystroke.
 * Mirrors the blur-commit pattern used by FilterValuesInput in the search builder.
 */
export function DecimalInput({
  value,
  normalize,
  onCommit,
  ...inputProps
}: {
  value: string;
  normalize: (raw: string) => string;
  onCommit: (normalized: string) => void;
} & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'onBlur'>) {
  const [raw, setRaw] = useState(value);

  // Resync when the committed value changes from outside (e.g. settings reload, a Clear button).
  useEffect(() => {
    setRaw(value);
  }, [value]);

  const commit = () => {
    const normalized = normalize(raw);
    onCommit(normalized);
    setRaw(normalized);
  };

  return (
    <input
      {...inputProps}
      value={raw}
      onChange={(event) => setRaw(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        inputProps.onKeyDown?.(event);
        if (!event.defaultPrevented && event.key === 'Enter') {
          event.currentTarget.blur();
        }
      }}
    />
  );
}
