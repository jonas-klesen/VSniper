// Plain <select> for choosing one of the configured AI models from the registry.
// Matches SettingsPage's existing label/select markup conventions — there is no
// custom dropdown/combobox primitive in this codebase.
import { aiModelLabel } from '../lib/aiModels';
import type { AiModelConfig } from '../types';

const NONE_VALUE = '';

export function ModelSelect({
  models,
  value,
  onChange,
  allowNone = false,
  label,
  helpText,
}: {
  models: AiModelConfig[];
  value: string | null;
  onChange: (id: string | null) => void;
  allowNone?: boolean;
  label?: string;
  helpText?: string;
}) {
  const select = (
    <select
      value={value ?? NONE_VALUE}
      onChange={(e) => onChange(e.target.value === NONE_VALUE ? null : e.target.value)}
    >
      {allowNone && <option value={NONE_VALUE}>none</option>}
      {models.map((model) => (
        <option key={model.id} value={model.id}>
          {aiModelLabel(model)}
        </option>
      ))}
    </select>
  );

  if (!label) return select;

  return (
    <label>
      {label}
      {select}
      {helpText ? <span className="field-help">{helpText}</span> : null}
    </label>
  );
}
