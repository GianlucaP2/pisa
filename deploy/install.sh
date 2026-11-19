#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Install Pisa on macOS, Linux or Raspberry Pi OS (64-bit) with Docker: a folder with the compose bundle, then
# `pisa init` run inside the Pisa image (the host needs no Python), which writes deploy/.env.
#
# It installs nothing else: without Docker it says where to get it and stops. Download it, read it, then run:
#
#   PISA_REPO=<owner>/<repo> PISA_VERSION=<tag or commit> bash install.sh [pisa init flags...]
#   bash deploy/install.sh [pisa init flags...]          # from a checkout: uses it in place, builds the image
#
#   PISA_REPO      the GitHub repository the release comes from, owner/name (no default)
#   PISA_VERSION   a release tag (v1.2.3) or a full commit id: nothing is fetched from a moving branch
#   PISA_HOME      the folder to install into (default: ~/pisa)
#   PISA_REGISTRY  the image registry (default: the release's, ghcr.io); PISA_IMAGE overrides the whole image
#   PISA_RAW_BASE  where the bundle's files are fetched from (default: https://raw.githubusercontent.com)
#   PISA_RELEASE_BASE  where the release's assets are fetched from (default: https://github.com)
#
# A release tag's bundle is checked against the release's SHA256SUMS (every file, or nothing is installed), and
# the image is written into .env by digest, from the release's IMAGES: ghcr.io/<owner>/pisa@sha256:<digest>,
# never a tag that can be moved. A commit id names its files' content itself; it has no release, so it needs
# PISA_IMAGE set to an image by digest (name@sha256:<digest>).
#   TS_AUTHKEY     tailscale mode: the auth key, read from the environment rather than a prompt
#
# Flags after the script's name go to `pisa init`: --mode tailscale|caddy|local, --domain, --email,
# --timezone, --yes, --force (see `pisa init --help`).
set -euo pipefail
umask 077

DOCKER_LINK="https://docs.docker.com/get-started/get-docker/"
PI_DOCKER_LINK="https://docs.docker.com/engine/install/debian/"
# The bundle, as paths in the repository; each lands at the same path under PISA_HOME.
BUNDLE=(deploy/docker-compose.yml deploy/postgres/init/01-roles.sh deploy/tailscale/serve.json deploy/caddy/Caddyfile
  .env.example)
BUNDLE+=(deploy/.env.example)  # read by people, not by compose
BUNDLE+=(deploy/tailscale/cert-export.sh)  # the tailscale sidecar's entrypoint

IMAGE_USER="10001:10001"  # the image's own user (core/Dockerfile)
IMAGE_RE='^[a-z0-9][a-z0-9.:-]*(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$'

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

check_platform() {
  case "$(uname -s)" in
    Darwin|Linux) ;;
    *) die "this script is for macOS and Linux; on Windows run install.ps1 in PowerShell." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64|arm64|aarch64) ;;
    armv6l|armv7l|armhf|i386|i686)
      die "Pisa's images are 64-bit only (amd64, arm64). On a Raspberry Pi, install Raspberry Pi OS (64-bit)." ;;
    *) die "unsupported processor $(uname -m): Pisa's images are for amd64 and arm64." ;;
  esac
}

check_docker() {
  local link="$DOCKER_LINK"
  if [ -r /proc/device-tree/model ] && grep -qa "Raspberry Pi" /proc/device-tree/model; then
    link="$PI_DOCKER_LINK (Raspberry Pi OS 64-bit follows the Debian instructions)"
  fi
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install it from $link, then run this again."
  docker compose version >/dev/null 2>&1 \
    || die "Docker Compose v2 (\`docker compose\`) is missing. See $link, then run this again."
  docker info >/dev/null 2>&1 \
    || die "Docker is installed but not running (or this user may not use it). Start Docker, then run this again."
}

fetch() {  # fetch <url> <file>
  if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --output "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget --quiet --https-only --output-document "$2" "$1"
  else
    die "neither curl nor wget is installed; install one, then run this again."
  fi
}

sha256_of() {  # sha256_of <file>: its SHA-256, in hex
  local sum
  if command -v sha256sum >/dev/null 2>&1; then
    sum="$(sha256sum < "$1")"
  elif command -v shasum >/dev/null 2>&1; then
    sum="$(shasum -a 256 < "$1")"
  else
    sum="$(openssl dgst -sha256 -r < "$1")"
  fi
  printf '%s' "${sum%% *}"
}

verify_bundle() {  # verify_bundle <dir> <SHA256SUMS>: every bundle file in <dir> has the hash the release lists
  local dir="$1" sums="$2" path want got
  for path in "${BUNDLE[@]}"; do
    want="$(awk -v p="$path" '($2 == p || $2 == "*" p) && $1 ~ /^[0-9a-f]+$/ && length($1) == 64 {
      print $1; exit }' "$sums")"
    [ -n "$want" ] || die "the release's SHA256SUMS does not list $path; nothing was installed."
    got="$(sha256_of "$dir/$path")"
    [ "$got" = "$want" ] \
      || die "$path does not match the release's SHA256SUMS (it may have been changed); nothing was installed."
  done
}

release_image() {  # release_image <IMAGES>: the server image by digest, as the release lists it
  local ref
  ref="$(awk '$1 == "pisa" { print $2; exit }' "$1")"
  [[ "$ref" =~ $IMAGE_RE ]] || die "the release's IMAGES has no server image by digest; nothing was installed."
  if [ -n "${PISA_REGISTRY:-}" ]; then
    ref="$PISA_REGISTRY/${ref#*/}"  # the same digest from a mirror
  fi
  printf '%s' "$ref"
}

fetch_bundle() {  # fetch_bundle <home>: the bundle, checked; sets RELEASE_IMAGE for a release tag
  local home="$1" base path tmp release
  [ -n "${PISA_REPO:-}" ] || die "set PISA_REPO to the repository the release comes from (owner/name)."
  case "$PISA_REPO" in
    */*) ;;
    *) die "PISA_REPO must be owner/name, not '$PISA_REPO'." ;;
  esac
  [ -n "${PISA_VERSION:-}" ] || die "set PISA_VERSION to a release tag (v1.2.3) or a full commit id."
  if ! [[ "$PISA_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$ || "$PISA_VERSION" =~ ^[0-9a-f]{40}$ ]]; then
    die "PISA_VERSION '$PISA_VERSION' is not pinned: use a release tag (v1.2.3) or a full 40-character commit id."
  fi
  base="${PISA_RAW_BASE:-https://raw.githubusercontent.com}/$PISA_REPO/$PISA_VERSION"
  tmp="$(mktemp -d)"
  FETCH_DIR="$tmp"
  trap 'rm -rf "${FETCH_DIR:-}"' EXIT
  for path in "${BUNDLE[@]}"; do
    mkdir -p "$tmp/$(dirname "$path")"
    fetch "$base/$path" "$tmp/$path" || die "could not fetch $path for $PISA_VERSION."
  done
  RELEASE_IMAGE=""
  if [[ "$PISA_VERSION" == v* ]]; then
    release="${PISA_RELEASE_BASE:-https://github.com}/$PISA_REPO/releases/download/$PISA_VERSION"
    mkdir -p "$tmp/.release"
    fetch "$release/SHA256SUMS" "$tmp/.release/SHA256SUMS" \
      || die "could not fetch the release's SHA256SUMS for $PISA_VERSION; nothing was installed."
    fetch "$release/IMAGES" "$tmp/.release/IMAGES" \
      || die "could not fetch the release's IMAGES for $PISA_VERSION; nothing was installed."
    verify_bundle "$tmp" "$tmp/.release/SHA256SUMS"
    RELEASE_IMAGE="$(release_image "$tmp/.release/IMAGES")"
    say "Checked the bundle against the release's SHA256SUMS."
  fi
  for path in "${BUNDLE[@]}"; do
    mkdir -p "$home/$(dirname "$path")"
    mv -f "$tmp/$path" "$home/$path"
  done
  chmod 0755 "$home/deploy/postgres/init/01-roles.sh"
  # the database, Tailscale and Caddy containers read them as their own users, with no capabilities
  chmod -R go+rX "$home/deploy/postgres" "$home/deploy/tailscale" "$home/deploy/caddy"
}

# deploy/tls: the local mode's certificate (read by the worker, uid 10001, for `pisa pair`) and its private key
# (0600, this user's: the api-lan service runs as this user, PISA_LAN_UID). The folder must let them through.
tls_dir() {  # tls_dir <deploy>
  mkdir -p "$1/tls"
  chmod 0755 "$1/tls"
}

main() {
  check_platform
  check_docker

  local here home deploy image=""
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$here/docker-compose.yml" ] && [ -d "$here/../core" ]; then
    home="$(cd "$here/.." && pwd)"  # a checkout: use it in place, the image is built from core/
    say "Using this checkout: $home"
  else
    if [ -z "${PISA_IMAGE:-}" ] && [[ "${PISA_VERSION:-}" =~ ^[0-9a-f]{40}$ ]]; then
      die "a commit id has no release to take the image from: set PISA_IMAGE to one by digest (name@sha256:...)."
    fi
    home="${PISA_HOME:-$HOME/pisa}"
    mkdir -p "$home"
    home="$(cd "$home" && pwd)"
    fetch_bundle "$home"
    image="${PISA_IMAGE:-$RELEASE_IMAGE}"
    [[ "$image" == *@sha256:* ]] || say "Note: PISA_IMAGE names a tag, not a digest: what it runs can change."
    say "Installed the compose bundle in $home"
  fi
  deploy="$home/deploy"
  tls_dir "$deploy"
  [ -e "$deploy/.env" ] || : > "$deploy/.env"  # compose wants the file; `pisa init` fills it
  chmod 0600 "$deploy/.env"

  local -a init_args=(--env-file /setup/.env --show-dir "$deploy")
  [ -z "$image" ] || init_args+=(--image "$image")
  # As this user, not the image's (uid 10001): the folder is this user's and stays so, and the files init
  # writes (.env, the local mode's TLS key) are this user's, readable by compose on this machine. Docker
  # Desktop (macOS) maps ownership anyway; the same uid there keeps one rule everywhere.
  # Run as root, the folder goes to the image's user first and init runs as that user: the files are created
  # as theirs (api-lan runs as them), and init never has to chown, which the container (no capabilities)
  # may not do. Root still reads them on this machine.
  local user
  user="$(id -u):$(id -g)"
  if [ "$(id -u)" = 0 ]; then
    user="$IMAGE_USER"
    chown -R "$IMAGE_USER" "$deploy"
  fi
  local -a run_args=(--rm --no-deps --volume "$deploy:/setup" --env TS_AUTHKEY --user "$user"
    --env "PISA_HOST_NAME=$(uname -n)")  # the default name in local mode: the container has its own

  # Placeholders for the compose file's required passwords, for this one command only: `pisa init` makes the
  # real ones, and nothing here starts the database.
  POSTGRES_PASSWORD=init PISA_MIGRATE_PASSWORD=init PISA_APP_PASSWORD=init PISA_IMAGE="${image:-${PISA_IMAGE:-}}" \
    docker compose -f "$deploy/docker-compose.yml" --env-file "$deploy/.env" \
    run "${run_args[@]}" api pisa init "${init_args[@]}" "$@"
}

# Run, unless sourced (the release workflow reads BUNDLE from this file).
if ! (return 0 2>/dev/null); then
  main "$@"
fi
