#!/bin/sh
# Materialise the basic-auth file nginx references from $BASIC_AUTH_USERS.
# Format matches Traefik / htpasswd: comma-separated "user:{SHA}base64(sha1(pw))"
# entries (nginx supports the {SHA} scheme). Generate with scripts/generate_auth.py.
#
# Fail closed: if no credentials are configured we refuse to start rather than
# serve the dashboard and secret-bearing /api unauthenticated.
set -e

HTPASSWD_FILE=/etc/nginx/.htpasswd

if [ -z "${BASIC_AUTH_USERS:-}" ]; then
    echo "[entrypoint] FATAL: BASIC_AUTH_USERS is not set — refusing to start without auth." >&2
    exit 1
fi

printf '%s\n' "$BASIC_AUTH_USERS" | tr ',' '\n' | sed '/^$/d' > "$HTPASSWD_FILE"
echo "[entrypoint] wrote $HTPASSWD_FILE with $(wc -l < "$HTPASSWD_FILE") user(s)"
