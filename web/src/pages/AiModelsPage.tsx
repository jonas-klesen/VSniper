import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { ErrorText } from '../components/ErrorText';
import { api } from '../lib/api';
import type { AiModelSettingsSavePayload } from '../lib/api';
import { AI_MODEL_PROVIDERS, REASONING_EFFORTS, aiModelLabel } from '../lib/aiModels';
import { queryKeys } from '../lib/queryKeys';
import { DecimalInput } from '../components/DecimalInput';
import { ModelSelect } from '../components/ModelSelect';
import type { AiModelConfig, AiModelCreate, AiModelProvider, ReasoningEffort } from '../types';

type ModelFormState = {
  provider: AiModelProvider;
  model_name: string;
  reasoning_effort: ReasoningEffort;
  local_base_url: string;
  display_name: string;
};

const EMPTY_FORM: ModelFormState = {
  provider: 'openai',
  model_name: '',
  reasoning_effort: 'medium',
  local_base_url: '',
  display_name: '',
};

const EMPTY_AI_SETTINGS: AiModelSettingsSavePayload = {
  vinted_region: 'de',
  judge_model_id: null,
  judge_fallback_model_id: null,
  learn_model_id: null,
  observation_model_id: null,
  vlm_grid_size: 1,
  vlm_pack_multiple_listing_images: true,
  vlm_judge_parallel_requests: 1,
  ai_judge_image_max_px: 512,
};

function parsePositiveNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function formFromModel(model: AiModelConfig): ModelFormState {
  return {
    provider: model.provider,
    model_name: model.model_name,
    reasoning_effort: model.reasoning_effort,
    local_base_url: model.local_base_url ?? '',
    display_name: model.display_name,
  };
}

export function AiModelsPage() {
  const queryClient = useQueryClient();
  const modelsQuery = useQuery({ queryKey: queryKeys.aiModels, queryFn: api.getAiModels });
  const settingsQuery = useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });

  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ModelFormState>(EMPTY_FORM);
  const [showCreate, setShowCreate] = useState(false);
  const [aiSettings, setAiSettings] = useState<AiModelSettingsSavePayload>(EMPTY_AI_SETTINGS);
  const [aiSettingsDirty, setAiSettingsDirty] = useState(false);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.aiModels });
    queryClient.invalidateQueries({ queryKey: queryKeys.settings });
  };

  useEffect(() => {
    if (!settingsQuery.data || aiSettingsDirty) return;
    setAiSettings({
      vinted_region: settingsQuery.data.vinted_region,
      judge_model_id: settingsQuery.data.judge_model_id,
      judge_fallback_model_id: settingsQuery.data.judge_fallback_model_id,
      learn_model_id: settingsQuery.data.learn_model_id,
      observation_model_id: settingsQuery.data.observation_model_id,
      vlm_grid_size: settingsQuery.data.vlm_grid_size,
      vlm_pack_multiple_listing_images: settingsQuery.data.vlm_pack_multiple_listing_images,
      vlm_judge_parallel_requests: settingsQuery.data.vlm_judge_parallel_requests,
      ai_judge_image_max_px: settingsQuery.data.ai_judge_image_max_px,
    });
  }, [settingsQuery.data, aiSettingsDirty]);

  const saveAiSettingsMutation = useMutation({
    mutationFn: api.saveAiModelSettings,
    onSuccess: (savedSettings) => {
      setAiSettingsDirty(false);
      queryClient.setQueryData(queryKeys.settings, savedSettings);
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
  });

  const createMutation = useMutation({
    mutationFn: (payload: AiModelCreate) => api.createAiModel(payload),
    onSuccess: () => {
      invalidate();
      setShowCreate(false);
      setForm(EMPTY_FORM);
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: AiModelCreate }) =>
      api.updateAiModel(id, {
        model_name: payload.model_name,
        reasoning_effort: payload.reasoning_effort,
        local_base_url: payload.provider === 'local' ? payload.local_base_url ?? null : null,
      }),
    onSuccess: () => {
      invalidate();
      setEditingId(null);
      setForm(EMPTY_FORM);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteAiModel(id),
    onSuccess: invalidate,
  });

  const [testModelId, setTestModelId] = useState<string>('');
  const [testPrompt, setTestPrompt] = useState('Reply with one short sentence if this model is reachable.');
  const testModelMutation = useMutation({
    mutationFn: () => api.testModel({ model_id: testModelId, prompt: testPrompt.trim() || 'Say hello.' }),
  });

  useEffect(() => {
    if (!testModelId && modelsQuery.data && modelsQuery.data.length > 0) {
      setTestModelId(modelsQuery.data[0].id);
    }
  }, [modelsQuery.data, testModelId]);

  const startEdit = (model: AiModelConfig) => {
    setEditingId(model.id);
    setForm(formFromModel(model));
    setShowCreate(false);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setShowCreate(false);
    setForm(EMPTY_FORM);
  };

  const submitCreate = () => {
    createMutation.mutate({
      provider: form.provider,
      model_name: form.model_name,
      reasoning_effort: form.reasoning_effort,
      local_base_url: form.provider === 'local' ? form.local_base_url || null : null,
    });
  };

  const submitEdit = () => {
    if (!editingId) return;
    updateMutation.mutate({
      id: editingId,
      payload: {
        provider: form.provider,
        model_name: form.model_name,
        reasoning_effort: form.reasoning_effort,
        local_base_url: form.local_base_url || null,
      },
    });
  };

  if (modelsQuery.isLoading || settingsQuery.isLoading) return <p>Loading AI models…</p>;
  if (modelsQuery.isError || !modelsQuery.data || settingsQuery.isError || !settingsQuery.data) {
    return <ErrorText error={modelsQuery.error ?? settingsQuery.error ?? 'Could not load AI models.'} />;
  }

  const models = modelsQuery.data;
  const setAiSetting = <K extends keyof AiModelSettingsSavePayload>(key: K, value: AiModelSettingsSavePayload[K]) => {
    if (saveAiSettingsMutation.isSuccess) saveAiSettingsMutation.reset();
    setAiSettingsDirty(true);
    setAiSettings((current) => ({ ...current, [key]: value }));
  };

  const renderForm = (mode: 'create' | 'edit') => (
    <section className="settings-subsection">
      <div>
        <h4>{mode === 'create' ? 'Add model' : 'Edit model'}</h4>
        <p className="muted">{mode === 'create' ? 'Register a new model in the AI model registry.' : 'Provider cannot be changed after creation.'}</p>
      </div>
      <div className="form-grid three-up">
        <label>
          Provider
          <select
            value={form.provider}
            onChange={(e) => setForm((f) => ({ ...f, provider: e.target.value as AiModelProvider }))}
            disabled={mode === 'edit'}
          >
            {AI_MODEL_PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label>
          Model name
          <input value={form.model_name} onChange={(e) => setForm((f) => ({ ...f, model_name: e.target.value }))} placeholder="gpt-5.4-mini" />
        </label>
        <label>
          Reasoning effort
          <select value={form.reasoning_effort} onChange={(e) => setForm((f) => ({ ...f, reasoning_effort: e.target.value as ReasoningEffort }))}>
            {REASONING_EFFORTS.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
        {form.provider === 'local' && (
          <label>
            Local base URL
            <input
              value={form.local_base_url}
              onChange={(e) => setForm((f) => ({ ...f, local_base_url: e.target.value }))}
              placeholder="http://127.0.0.1:8080/v1"
            />
          </label>
        )}
      </div>
      <div className="button-row" style={{ marginTop: '0.75rem' }}>
        <button
          onClick={mode === 'create' ? submitCreate : submitEdit}
          disabled={!form.model_name.trim() || createMutation.isPending || updateMutation.isPending}
        >
          {mode === 'create'
            ? (createMutation.isPending ? 'Adding…' : 'Add model')
            : (updateMutation.isPending ? 'Saving…' : 'Save changes')}
        </button>
        <button className="secondary" onClick={cancelEdit}>Cancel</button>
      </div>
      <ErrorText error={mode === 'create' ? createMutation.error : updateMutation.error} prefix="Save failed" />
    </section>
  );

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Configuration</p>
          <h2>AI Models</h2>
          <p className="muted">The model registry used by Judge, Judge fallback, Learn, and Observation in Settings.</p>
        </div>
        {!showCreate && !editingId && (
          <button onClick={() => { setShowCreate(true); setForm(EMPTY_FORM); }}>Add model</button>
        )}
      </div>

      <article className="card">
        <h3>AI configuration</h3>
        <p className="muted">Choose the models and control how candidate images are sent for judging.</p>
        <div className="settings-subgrid">
          <section className="settings-subsection">
            <div>
              <h4>Models by task</h4>
              <p className="muted">These assignments take effect for future scans and taste recomputes.</p>
            </div>
            <div className="form-grid">
              <ModelSelect
                label="Judge model"
                models={models}
                value={aiSettings.judge_model_id}
                onChange={(id) => setAiSetting('judge_model_id', id)}
                helpText="Primary model used to score every fetched candidate."
              />
              <ModelSelect
                label="Judge fallback model"
                models={models}
                value={aiSettings.judge_fallback_model_id}
                onChange={(id) => setAiSetting('judge_fallback_model_id', id)}
                allowNone
                helpText="Used only when the primary judge model fails."
              />
              <ModelSelect
                label="Profile synthesis model"
                models={models}
                value={aiSettings.learn_model_id}
                onChange={(id) => setAiSetting('learn_model_id', id)}
                helpText="Model used during taste recompute."
              />
              <ModelSelect
                label="Observation model"
                models={models}
                value={aiSettings.observation_model_id}
                onChange={(id) => setAiSetting('observation_model_id', id)}
                helpText="Model used to describe wardrobe and offer reference photos."
              />
            </div>
          </section>

          <section className="settings-subsection">
            <div>
              <h4>Judge invocation</h4>
              <p className="muted">Controls how listing photos are packed and sent to the judge model.</p>
            </div>
            <div className="form-grid three-up">
              <label>
                VLM grid
                <select value={aiSettings.vlm_grid_size} onChange={(e) => setAiSetting('vlm_grid_size', Number(e.target.value))}>
                  <option value={1}>1x1</option>
                  <option value={4}>2x2</option>
                  <option value={9}>3x3</option>
                </select>
              </label>
              <label>
                Parallel requests
                <DecimalInput
                  type="text"
                  inputMode="numeric"
                  value={String(aiSettings.vlm_judge_parallel_requests)}
                  normalize={(raw) => String(Math.max(1, Math.min(16, Math.round(parsePositiveNumber(raw, aiSettings.vlm_judge_parallel_requests)))))}
                  onCommit={(normalized) => setAiSetting('vlm_judge_parallel_requests', Number(normalized))}
                />
              </label>
              <label>
                Tile size per image (px)
                <DecimalInput
                  type="text"
                  inputMode="numeric"
                  value={String(aiSettings.ai_judge_image_max_px)}
                  normalize={(raw) => String(Math.max(64, Math.min(2048, Math.round(parsePositiveNumber(raw, aiSettings.ai_judge_image_max_px)))))}
                  onCommit={(normalized) => setAiSetting('ai_judge_image_max_px', Number(normalized))}
                />
                <span className="field-help">Max candidate tile dimension. A 2x2 grid at 512 px produces a roughly 1096 px sheet.</span>
              </label>
            </div>
            <label className="checkbox-label settings-checkbox-card" style={{ marginTop: '1rem' }}>
              <input
                type="checkbox"
                checked={aiSettings.vlm_pack_multiple_listing_images}
                onChange={(e) => setAiSetting('vlm_pack_multiple_listing_images', e.target.checked)}
              />
              <span>
                Pack multiple listing photos
                <small>Use up to four photos inside each candidate tile while keeping the tile size fixed.</small>
              </span>
            </label>
          </section>
        </div>
        <div className="button-row" style={{ marginTop: '1rem' }}>
          <button onClick={() => saveAiSettingsMutation.mutate(aiSettings)} disabled={saveAiSettingsMutation.isPending || !aiSettingsDirty}>
            {saveAiSettingsMutation.isPending ? 'Saving…' : 'Save AI configuration'}
          </button>
          {saveAiSettingsMutation.isSuccess && <span className="muted">Saved.</span>}
        </div>
        <ErrorText error={saveAiSettingsMutation.error} prefix="Save failed" />
      </article>

      <article className="card">
        <h3>Registered models</h3>
        {models.length === 0 ? (
          <p className="muted">No models registered yet.</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr className="muted">
                  <th>Display name</th>
                  <th>Provider</th>
                  <th>Model</th>
                  <th>Reasoning effort</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {models.map((model) => (
                  <tr key={model.id}>
                    <td>{model.display_name}</td>
                    <td>{model.provider}</td>
                    <td>{model.model_name}</td>
                    <td>{model.reasoning_effort}</td>
                    <td>
                      <div className="button-row compact-actions">
                        <button className="secondary" onClick={() => startEdit(model)}>Edit</button>
                        <button
                          className="secondary"
                          onClick={() => deleteMutation.mutate(model.id)}
                          disabled={deleteMutation.isPending && deleteMutation.variables === model.id}
                        >
                          {deleteMutation.isPending && deleteMutation.variables === model.id ? 'Deleting…' : 'Delete'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ErrorText error={deleteMutation.error} prefix="Delete failed" />

        {showCreate && renderForm('create')}
        {editingId && renderForm('edit')}
      </article>

      <article className="card">
        <h3>Model access test</h3>
        <div className="settings-subgrid">
          <section className="settings-subsection">
            <div>
              <h4>Access test</h4>
              <p className="muted">Sends a text prompt to any registered model and shows the raw response.</p>
            </div>
            <div className="form-grid">
              <label>
                Model
                <select value={testModelId} onChange={(e) => setTestModelId(e.target.value)}>
                  {models.length === 0 && <option value="">No models registered</option>}
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>{aiModelLabel(m)} ({m.provider})</option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              Test prompt
              <textarea
                rows={3}
                value={testPrompt}
                onChange={(e) => setTestPrompt(e.target.value)}
              />
            </label>
            <div className="button-row" style={{ marginTop: '0.75rem' }}>
              <button
                type="button"
                onClick={() => testModelMutation.mutate()}
                disabled={testModelMutation.isPending || !testModelId}
              >
                {testModelMutation.isPending ? 'Testing…' : 'Test model'}
              </button>
              {testModelMutation.isSuccess && (
                <span className="muted">
                  {testModelMutation.data.model} ({testModelMutation.data.provider})
                  {testModelMutation.data.base_url ? ` at ${testModelMutation.data.base_url}` : ''}
                </span>
              )}
            </div>
            <ErrorText error={testModelMutation.error} prefix="Model test failed" />
            {testModelMutation.data ? (
              <pre className="preformatted">{testModelMutation.data.answer}</pre>
            ) : null}
          </section>
        </div>
      </article>
    </section>
  );
}
