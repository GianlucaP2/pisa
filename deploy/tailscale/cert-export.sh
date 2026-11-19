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
set -u

OUT_DIR=/ts-cert
KEY_TMP=/tmp/ts-serve.key

/usr/local/bin/containerboot &
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

(
  while kill -0 "$boot" 2>/dev/null; do
    if export_cert; then sleep 43200; else sleep 60; fi
  done
) &

wait "$boot"
status=$?
if kill -0 "$boot" 2>/dev/null; then  # a signal ended the first wait: wait for containerboot to stop
  wait "$boot"
  status=$?
fi
exit "$status"
