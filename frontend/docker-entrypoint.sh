#!/bin/sh
set -e

CERT_DIR=/certs
CERT_FILE="$CERT_DIR/tls.crt"
KEY_FILE="$CERT_DIR/tls.key"

mkdir -p /var/log/snapman

if [ "$ENABLE_HTTPS" = "true" ]; then
    mkdir -p "$CERT_DIR"
    if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
        echo "[snapman] ENABLE_HTTPS=true but no cert found at $CERT_FILE / $KEY_FILE -- generating a self-signed one." >&2
        echo "[snapman] This is fine for eval/internal use. For a customer-trusted cert, put your own" >&2
        echo "[snapman] tls.crt and tls.key in the ./certs directory (bind-mounted to $CERT_DIR) and restart." >&2
        openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
            -keyout "$KEY_FILE" -out "$CERT_FILE" \
            -subj "/CN=snapman" >/dev/null 2>&1
    fi
    cp /etc/nginx/https.conf /etc/nginx/conf.d/default.conf
else
    cp /etc/nginx/http.conf /etc/nginx/conf.d/default.conf
fi

exec nginx -g 'daemon off;'
