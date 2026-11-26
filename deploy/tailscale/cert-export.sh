#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# The `tailscale` profile's entrypoint (deploy/docker-compose.yml): Tailscale's own containerboot, as the image
# runs it, plus an export of the certificate `tailscale serve` presents, so `pisa pair` can put its key's pin in
# the pairing QR (PISA_TLS_CERT=/etc/pisa/ts-cert/cert.pem in the Pisa containers, read-only).
#
# `tailscale cert` asks tailscaled for the domain's certificate through the same cache `serve` uses (it is
# renewed there, not here). Only the certificate leaves this container: the key `tailscale cert` also writes
# goes to /tmp (a tmpfs of this container only) and is deleted at once. The file on the shared volume is
# replaced atomically. Every 12 hours, or every minute until the first certificate arrives (HTTPS certificates
# must be enabled for the tailnet).
#
# The node's tailnet IPv4 address (`tailscale ip -4`, the first one) goes beside it, in /ts-cert/ip (0644,
# replaced atomically, rewritten when it changes): the API offers it as the talk's ICE host
# (PISA_ICE_HOST_FILE=/etc/pisa/ts-cert/ip), so PISA_ICE_HOST stays empty. Every minute until the node has
# logged in and the first write succeeds, then every 12 hours.
#
# CERT_EXPORT_DIR, CERT_EXPORT_BOOT and CERT_EXPORT_TICK (the seconds of a minute) are for the tests only.
set -u

OUT_DIR="${CERT_EXPORT_DIR:-/ts-cert}"
BOOT="${CERT_EXPORT_BOOT:-/usr/local/bin/containerboot}"
TICK="${CERT_EXPORT_TICK:-60}"
KEY_TMP=/tmp/ts-serve.key

"$BOOT" &
boot=$!
trap 'kill -TERM "$boot" 2>/dev/null' TERM INT

cert_domain() {
  tailscale --socket="$TS_SOCKET" status --json 2>/dev/null | tr -d ' \t\n' \
    | sed -n 's/.*"CertDomains":\["\([^"]*\)".*/\1/p'
}

export_cert() {
  domain="$(cert_domain)"
  [ -n "$domain" ] || return 1
  tmp="$OUT_DIR/.cert.pem.tmp"
  rm -f "$tmp" "$KEY_TMP"
  if tailscale --socket="$TS_SOCKET" cert --cert-file "$tmp" --key-file "$KEY_TMP" "$domain" >/dev/null 2>&1; then
    rm -f "$KEY_TMP"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$OUT_DIR/cert.pem"
    return 0
  fi
  rm -f "$tmp" "$KEY_TMP"
  return 1
}

export_ip() {
  ip="$(tailscale --socket="$TS_SOCKET" ip -4 2>/dev/null | head -n 1 | tr -d ' \t\r')"
  case "$ip" in
    '' | *[!0-9.]*) return 1 ;;
  esac
  [ "$(cat "$OUT_DIR/ip" 2>/dev/null)" = "$ip" ] && return 0
  tmp="$OUT_DIR/.ip.tmp"
  rm -f "$tmp"
  if printf '%s\n' "$ip" > "$tmp" && chmod 0644 "$tmp" && mv -f "$tmp" "$OUT_DIR/ip"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# minutes until each export's next try: 720 (12 hours) after a success, 1 after a failure
(
  cert_wait=0
  ip_wait=0
  while kill -0 "$boot" 2>/dev/null; do
    if [ "$cert_wait" -le 0 ]; then
      if export_cert; then cert_wait=720; else cert_wait=1; fi
    fi
    if [ "$ip_wait" -le 0 ]; then
      if export_ip; then ip_wait=720; else ip_wait=1; fi
    fi
    sleep "$TICK"
    cert_wait=$((cert_wait - 1))
    ip_wait=$((ip_wait - 1))
  done
) &

wait "$boot"
status=$?
if kill -0 "$boot" 2>/dev/null; then  # a signal ended the first wait: wait for containerboot to stop
  wait "$boot"
  status=$?
fi
exit "$status"
