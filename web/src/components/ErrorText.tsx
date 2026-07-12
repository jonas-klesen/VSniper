// Renders an inline, accessible error message for a failed query/mutation.
// Renders nothing when there is no error, so it can be dropped in unconditionally:
//   <ErrorText error={mutation.error} prefix="Save failed" />
import { ApiError } from '../lib/api';

export function ErrorText({ error, prefix }: { error: unknown; prefix?: string }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  const meta = error instanceof ApiError
    ? `HTTP ${error.status}${error.path ? ` · ${error.path}` : ''}`
    : '';
  return (
    <div className="error-text" role="alert">
      {meta ? <div className="error-text__meta">{meta}</div> : null}
      <div>{prefix ? `${prefix}: ` : ''}{message}</div>
    </div>
  );
}
