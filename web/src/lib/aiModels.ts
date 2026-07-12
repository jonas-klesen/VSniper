import type { AiModelConfig, ReasoningEffort } from '../types';

export const REASONING_EFFORTS: ReasoningEffort[] = ['low', 'medium', 'high'];

export const AI_MODEL_PROVIDERS: AiModelConfig['provider'][] = ['openai', 'cerebras', 'local', 'openrouter'];

export function aiModelLabel(model: AiModelConfig): string {
  return model.display_name || `${model.provider}: ${model.model_name}`;
}

// Resolves an AiModelConfig id (as stored on SettingsSnapshot.judge_model_id etc.)
// to a human-readable label, given the full registry list.
export function modelLabel(models: AiModelConfig[], modelId: string | null): string {
  if (!modelId) return 'none';
  const model = models.find((m) => m.id === modelId);
  return model ? aiModelLabel(model) : 'unknown model';
}
