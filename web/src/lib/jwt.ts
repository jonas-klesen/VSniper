import { formatBerlinDateTime } from './datetime';

/** Read a single cookie value by name, splitting only on the FIRST '=' so values that
 *  themselves contain '=' (e.g. base64 padding) are preserved intact. */
function extractCookieValue(cookie: string, name: string): string | null {
  for (const part of cookie.split(';')) {
    const trimmed = part.trim();
    const eq = trimmed.indexOf('=');
    if (eq === -1) continue;
    if (trimmed.slice(0, eq) === name) {
      const value = trimmed.slice(eq + 1);
      return value || null;
    }
  }
  return null;
}

/** Decode the `exp` claim from a JWT and return it as a Date, or null if absent/invalid. */
function decodeTokenExpiry(token: string): Date | null {
  const segments = token.split('.');
  if (segments.length < 2) return null;

  const payload = segments[1];
  const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4);

  try {
    const decoded = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
    const json = JSON.parse(decoded);
    if (typeof json.exp === 'number') {
      return new Date(json.exp * 1000);
    }
  } catch {
    return null;
  }

  return null;
}

export function decodeJwtExpiry(cookie: string): Date | null {
  if (!cookie) return null;
  // Accept either a full cookie string or a bare token. A bare JWT has dots but no '='.
  const token = cookie.includes('=') ? extractCookieValue(cookie, 'access_token_web') : cookie.trim();
  if (!token || !token.includes('.')) return null;
  return decodeTokenExpiry(token);
}

export function extractRefreshToken(cookie: string): string | null {
  if (!cookie) return null;
  return extractCookieValue(cookie, 'refresh_token_web');
}

export function decodeRefreshTokenExpiry(cookie: string): Date | null {
  const refreshToken = extractRefreshToken(cookie);
  if (!refreshToken) return null;
  return decodeTokenExpiry(refreshToken);
}

export function formatCookieExpiry(expiry: Date | null): string {
  if (!expiry) return '';
  const now = new Date();
  const isExpired = expiry <= now;
  const formatted = formatBerlinDateTime(expiry);
  return isExpired ? `Expired ${formatted}` : `Expires ${formatted}`;
}
