import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { api } from '../lib/api';
import type { SettingsSavePayload } from '../lib/api';
import { formatBerlinDateTime } from '../lib/datetime';
import { queryKeys } from '../lib/queryKeys';
import { decodeJwtExpiry, decodeRefreshTokenExpiry, extractRefreshToken, formatCookieExpiry } from '../lib/jwt';
import { useUnsavedGuard } from '../lib/useUnsavedGuard';
import { DecimalInput } from '../components/DecimalInput';
import { ErrorText } from '../components/ErrorText';

type SettingsFormState = SettingsSavePayload;

function parsePositiveNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
  const webhookQuery = useQuery({ queryKey: queryKeys.telegramWebhook, queryFn: api.getTelegramWebhookStatus });
  // `telegramPreview` is a POST, so it must be a mutation rather than a passive
  // query (a query would re-fire the side-effecting endpoint on focus/refetch).
  const previewMutation = useMutation({ mutationFn: api.telegramPreview });
  const previewRequested = useRef(false);

  const [form, setForm] = useState<SettingsFormState>({
    vinted_region: 'de',
    vinted_cookie: '',
    vinted_refresh_token: '',
    telegram_bot_token: '',
    telegram_chat_id: '',
    telegram_webhook_url: '',
    telegram_webhook_secret: '',
    alert_threshold: 9,
    scan_interval_seconds: 1800,
  });
  const formDirty = useRef(false);
  const [dirty, setDirty] = useState(false);
  const formRef = useRef(form);
  const setFormDirty = (value: boolean) => { formDirty.current = value; setDirty(value); };
  useUnsavedGuard(dirty);

  useEffect(() => { formRef.current = form; }, [form]);

  useEffect(() => {
    if (saveMutation.isSuccess) saveMutation.reset();
  }, [form]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!settingsQuery.data || formDirty.current) return;
    setForm({
      vinted_region: 'de',
      vinted_cookie: settingsQuery.data.vinted_cookie,
      vinted_refresh_token: settingsQuery.data.vinted_refresh_token,
      telegram_bot_token: settingsQuery.data.telegram_bot_token,
      telegram_chat_id: settingsQuery.data.telegram_chat_id,
      telegram_webhook_url: settingsQuery.data.telegram_webhook_url,
      telegram_webhook_secret: settingsQuery.data.telegram_webhook_secret,
      alert_threshold: settingsQuery.data.alert_threshold,
      scan_interval_seconds: settingsQuery.data.scan_interval_seconds,
    });
  }, [settingsQuery.data]);

  useEffect(() => {
    // Fetch the alert preview once on mount (guarded against React 18 strict-mode
    // double-invocation). The user can refresh it on demand via the button below.
    if (previewRequested.current) return;
    previewRequested.current = true;
    previewMutation.mutate();
  }, []);

  useEffect(() => {
    if (!webhookQuery.data || formDirty.current || formRef.current.telegram_webhook_url) return;
    setForm((current) => ({
      ...current,
      telegram_webhook_url: webhookQuery.data.configured_url ?? webhookQuery.data.effective_url ?? '',
    }));
  }, [webhookQuery.data]);

  const saveMutation = useMutation({
    mutationFn: api.saveSettings,
    onSuccess: (_result, submittedForm) => {
      if (JSON.stringify(formRef.current) === JSON.stringify(submittedForm)) {
        setFormDirty(false);
      }
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
  });

  const registerWebhookMutation = useMutation({
    mutationFn: async () => {
      const saved = await api.saveSettings(formRef.current);
      const status = await api.registerTelegramWebhook({
        url: formRef.current.telegram_webhook_url || undefined,
        drop_pending_updates: false,
      });
      return { saved, status };
    },
    onSuccess: () => {
      setFormDirty(false);
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
      queryClient.invalidateQueries({ queryKey: queryKeys.telegramWebhook });
    },
  });

  const sendTestMutation = useMutation({
    mutationFn: async () => {
      const saved = await api.saveSettings(formRef.current);
      const sent = await api.sendTelegramTest();
      return { saved, sent };
    },
    onSuccess: () => {
      setFormDirty(false);
      queryClient.invalidateQueries({ queryKey: queryKeys.settings });
      queryClient.invalidateQueries({ queryKey: queryKeys.telegramWebhook });
    },
  });

  const [cookieValidation, setCookieValidation] = useState<{
    status: 'idle' | 'validating' | 'healthy' | 'warning' | 'missing';
    detail: string;
  }>({ status: 'idle', detail: '' });
  const validateTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cookieExpiry = decodeJwtExpiry(form.vinted_cookie);
  const cookieExpiryText = formatCookieExpiry(cookieExpiry);
  const refreshTokenExpiry = decodeRefreshTokenExpiry(form.vinted_cookie) || decodeRefreshTokenExpiry(form.vinted_refresh_token ? `refresh_token_web=${form.vinted_refresh_token}` : '');
  const refreshTokenExpiryText = formatCookieExpiry(refreshTokenExpiry);

  useEffect(() => {
    if (validateTimeoutRef.current) {
      clearTimeout(validateTimeoutRef.current);
    }

    if (!form.vinted_cookie || form.vinted_cookie.trim() === '' || form.vinted_cookie === 'put-your-vinted-cookie-here') {
      setCookieValidation({ status: 'idle', detail: '' });
      return;
    }

    // Skip live validation when the cookie hasn't changed from the saved value — the
    // settings query hydrating the form on every visit would otherwise fire a network call.
    if (form.vinted_cookie === settingsQuery.data?.vinted_cookie) {
      setCookieValidation({ status: 'idle', detail: '' });
      return;
    }

    setCookieValidation({ status: 'validating', detail: 'Validating...' });

    validateTimeoutRef.current = setTimeout(async () => {
      try {
        const result = await api.validateCookie(form.vinted_cookie);
        setCookieValidation({ status: result.status, detail: result.detail });
      } catch (err) {
        setCookieValidation({ status: 'warning', detail: err instanceof Error ? err.message : 'Validation failed' });
      }
    }, 600);

    return () => {
      if (validateTimeoutRef.current) {
        clearTimeout(validateTimeoutRef.current);
      }
    };
  }, [form.vinted_cookie]);

  const validationColor = {
    idle: 'inherit',
    validating: '#888',
    healthy: '#2d8',
    warning: '#fa3',
    missing: '#f55',
  }[cookieValidation.status];

  // Only the settings form is essential to render. Telegram preview/webhook are
  // supplementary — they must not block editing the config that fixes them.
  if (settingsQuery.isLoading) return <p>Loading settings…</p>;
  if (settingsQuery.isError || !settingsQuery.data) {
    return <ErrorText error={settingsQuery.error ?? 'Could not load settings.'} />;
  }

  const settings = settingsQuery.data;
  const webhook = webhookQuery.data;

  const set = <K extends keyof SettingsFormState>(key: K, value: SettingsFormState[K]) => {
    setFormDirty(true);
    setForm((f) => ({ ...f, [key]: value }));
  };

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Configuration</p>
          <h2>Settings</h2>
        </div>
      </div>

      <article className="card">
        <h3>Vinted settings</h3>
        <div className="settings-groups">

          <div className="settings-group">
            <span className="settings-group-label">Vinted session</span>
            <div className="form-grid vinted-session-grid">
              <label>
                Vinted cookie
                <input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={form.vinted_cookie}
                  onChange={(e) => {
                    const value = e.target.value;
                    const refreshToken = extractRefreshToken(value);
                    set('vinted_cookie', value);
                    if (refreshToken) {
                      set('vinted_refresh_token', refreshToken);
                    }
                  }}
                  placeholder="Paste your Vinted cookie here"
                />
              </label>
              <div className="vinted-session-status" aria-live="polite">
                {cookieExpiryText && (
                  <div style={{ color: cookieExpiry && cookieExpiry <= new Date() ? '#f55' : '#888' }}>
                    Access token: {cookieExpiryText}
                  </div>
                )}
                {refreshTokenExpiryText && (
                  <div style={{ color: refreshTokenExpiry && refreshTokenExpiry <= new Date() ? '#f55' : '#2d8' }}>
                    Refresh token: {refreshTokenExpiryText}
                  </div>
                )}
                {cookieValidation.status !== 'idle' && (
                  <div style={{ color: validationColor }}>
                    <strong>{cookieValidation.status}:</strong> {cookieValidation.detail}
                  </div>
                )}
                <div className="field-help">
                  {settings.session_health.detail}
                </div>
              </div>
            </div>
          </div>

          <div className="settings-group">
            <span className="settings-group-label">Scanning</span>
            <div className="form-grid">
              <label>
                Scan interval (minutes)
                <DecimalInput
                  type="text"
                  inputMode="numeric"
                  value={String(form.scan_interval_seconds / 60)}
                  normalize={(raw) =>
                    String(Math.max(0.5, Math.min(1440, parsePositiveNumber(raw, form.scan_interval_seconds / 60))))
                  }
                  onCommit={(normalized) => set('scan_interval_seconds', Math.round(Number(normalized) * 60))}
                />
                <span className="field-help">
                  How often each enabled search gets rescanned. Searches are staggered evenly across this window
                  (e.g. 5 searches over 30 minutes means one every 6 minutes) to reduce blocking risk. Takes effect
                  within the worker's next poll, no restart needed.
                </span>
              </label>
              <label>
                Alert threshold
                <DecimalInput
                  type="text"
                  inputMode="numeric"
                  value={String(form.alert_threshold)}
                  normalize={(raw) => String(Math.max(1, Math.min(100, Math.round(parsePositiveNumber(raw, form.alert_threshold)))))}
                  onCommit={(normalized) => set('alert_threshold', Number(normalized))}
                />
                <span className="field-help">Candidates alert when the judge score is this value or higher. Configure AI models and judge invocation on the AI Models page.</span>
              </label>
            </div>
          </div>

        </div>
        <div className="button-row" style={{ marginTop: '1rem' }}>
          <button onClick={() => saveMutation.mutate(form)} disabled={saveMutation.isPending}>
            {saveMutation.isPending ? 'Saving…' : 'Save Vinted settings'}
          </button>
          {saveMutation.isSuccess && <span className="muted">Saved.</span>}
        </div>
        <ErrorText error={saveMutation.error} prefix="Save failed" />
      </article>

      <article className="card settings-main-group">
        <h3>Telegram settings</h3>
        <div className="settings-group">
          <div className="form-grid">
            <label>
              Bot token
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={form.telegram_bot_token}
                onChange={(e) => set('telegram_bot_token', e.target.value)}
                placeholder="123456789:AA..."
              />
            </label>
            <label>
              Chat ID
              <input
                value={form.telegram_chat_id}
                onChange={(e) => set('telegram_chat_id', e.target.value)}
                placeholder="523167654 or -100..."
              />
            </label>
            <label>
              Webhook URL
              <input
                value={form.telegram_webhook_url}
                onChange={(e) => set('telegram_webhook_url', e.target.value)}
                placeholder="https://your-public-host/api/telegram/webhook"
              />
            </label>
            <label>
              Webhook secret
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={form.telegram_webhook_secret}
                onChange={(e) => set('telegram_webhook_secret', e.target.value)}
                placeholder="A long random secret"
              />
            </label>
          </div>
          <div className="button-row" style={{ marginTop: '1rem' }}>
            <button onClick={() => saveMutation.mutate(form)} disabled={saveMutation.isPending}>
              {saveMutation.isPending ? 'Saving…' : 'Save Telegram settings'}
            </button>
            {saveMutation.isSuccess && <span className="muted">Saved.</span>}
          </div>
          <ErrorText error={saveMutation.error} prefix="Save failed" />
        </div>

        <div className="settings-group">
          <div className="telegram-panel-header">
            <div>
              <h4>Telegram webhook</h4>
              {webhook
                ? <p><strong>{webhook.is_registered ? 'registered' : 'not registered'}</strong> — {webhook.detail}</p>
                : <p className="muted">{webhookQuery.isError ? 'Webhook status unavailable.' : 'Loading webhook status…'}</p>}
            </div>
            <div className="button-row compact-actions">
              <button onClick={() => queryClient.invalidateQueries({ queryKey: queryKeys.telegramWebhook })}>
                Refresh status
              </button>
              <button
                onClick={() => registerWebhookMutation.mutate()}
                disabled={registerWebhookMutation.isPending || saveMutation.isPending || sendTestMutation.isPending}
              >
                {registerWebhookMutation.isPending ? 'Saving and registering…' : 'Save and register webhook'}
              </button>
              <button
                onClick={() => sendTestMutation.mutate()}
                disabled={sendTestMutation.isPending || saveMutation.isPending || registerWebhookMutation.isPending}
              >
                {sendTestMutation.isPending ? 'Sending…' : 'Save and send test notification'}
              </button>
            </div>
          </div>
          {webhook ? (
            <div className="telegram-status-grid">
              <div><span>Configured URL</span><strong>{webhook.configured_url ?? 'not set'}</strong></div>
              <div><span>Live Telegram URL</span><strong>{webhook.effective_url ?? 'not registered'}</strong></div>
              <div><span>URL match</span><strong>{webhook.matches_configured_url ? 'yes' : 'no'}</strong></div>
              <div><span>Secret token</span><strong>{webhook.has_secret_token ? 'configured' : 'not set'}</strong></div>
              <div><span>Pending updates</span><strong>{webhook.pending_update_count}</strong></div>
              <div><span>Allowed updates</span><strong>{webhook.allowed_updates.length ? webhook.allowed_updates.join(', ') : 'Telegram default'}</strong></div>
              <div><span>Last checked</span><strong>{formatBerlinDateTime(webhook.checked_at, 'not checked yet')}</strong></div>
              <div><span>Last Telegram error</span><strong>{webhook.last_error_message ?? 'none reported'}</strong></div>
            </div>
          ) : (
            <ErrorText error={webhookQuery.error} prefix="Webhook status failed to load" />
          )}
          <ErrorText error={registerWebhookMutation.error} prefix="Webhook registration failed" />
          <ErrorText error={sendTestMutation.error} prefix="Test notification failed" />
          {registerWebhookMutation.isSuccess && <p className="muted">Webhook was saved locally and registered with Telegram.</p>}
          {sendTestMutation.isSuccess && <p className="muted">Test notification sent to Telegram chat {sendTestMutation.data.sent.chat_id}.</p>}
        </div>

        <div className="settings-group">
          <div className="row-between">
            <span className="settings-group-label">Current alert preview</span>
            <button
              className="secondary"
              style={{ padding: '0.2rem 0.6rem', fontSize: '0.78rem' }}
              onClick={() => previewMutation.mutate()}
              disabled={previewMutation.isPending}
            >
              {previewMutation.isPending ? 'Refreshing…' : 'Refresh preview'}
            </button>
          </div>
          {previewMutation.data
            ? <pre className="preformatted">{previewMutation.data.preview}</pre>
            : <p className="muted">{previewMutation.isPending ? 'Loading preview…' : 'No preview available.'}</p>}
          <ErrorText error={previewMutation.error} prefix="Preview failed" />
        </div>
      </article>
    </section>
  );
}
