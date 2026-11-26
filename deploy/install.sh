#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Install Pisa on macOS, Linux or Raspberry Pi OS (64-bit) with Docker: a folder with the compose bundle, then
# `pisa init` run inside the Pisa image (the host needs no Python), which writes deploy/.env.
#
# It installs nothing else: without Docker it says where to get it and stops. Download it, read it, then run:
#
#   PISA_REPO=<owner>/<repo> PISA_VERSION=<tag or commit> bash install.sh [pisa init flags...]
#   curl -fsSL <raw URL of install.sh> | PISA_REPO=<owner>/<repo> PISA_VERSION=<tag> bash   # piped: works too,
#                  `pisa init` then asks its questions on the terminal (/dev/tty), or pass --yes
#   bash deploy/install.sh [pisa init flags...]          # from a checkout: uses it in place, builds the image
#
#   PISA_REPO      the GitHub repository the release comes from, owner/name (no default)
#   PISA_VERSION   a release tag (v1.2.3) or a full commit id: nothing is fetched from a moving branch
#   PISA_HOME      the folder to install into (default: ~/pisa)
#   PISA_REGISTRY  the image registry (default: the release's, ghcr.io); PISA_IMAGE overrides the whole image
#   PISA_RAW_BASE  where the bundle's files are fetched from (default: https://raw.githubusercontent.com)
#   PISA_RELEASE_BASE  where the release's assets are fetched from (default: https://github.com)
#   PISA_GITHUB_TOKEN  optional: a GitHub token, to install a release of a private repository. The bundle is then
#                  fetched with it (an Authorization header), and the release's assets through the GitHub API
#                  (PISA_API_BASE, default https://api.github.com), since a private release's download links take
#                  no token. It needs curl. It is never printed, never passed on a command line (curl reads the
#                  header from a 0600 file in the script's temporary folder) and never written to .env; the
#                  script drops it once the bundle is fetched. Without it, nothing changes.
#
# A release tag's bundle is checked against the release's SHA256SUMS (every file, or nothing is installed), and
# the image is written into .env by digest, from the release's IMAGES: ghcr.io/<owner>/pisa@sha256:<digest>,
# never a tag that can be moved. A commit id names its files' content itself; it has no release, so it needs
# PISA_IMAGE set to an image by digest (name@sha256:<digest>).
#   TS_AUTHKEY     tailscale mode: the auth key, read from the environment rather than a prompt
#
# Flags after the script's name go to `pisa init`: --mode tailscale|caddy|local, --domain, --email,
# --timezone, --country, --yes, --force (see `pisa init --help`), except one, the script's own:
#   --no-start     write the .env only; don't start Pisa (by default the script runs `docker compose up -d`
#                  once init has written it, waits until every service is healthy, then prints the next steps)
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
  docker info >/dev/null 2>&1 || die "$(docker_not_running)"
}

docker_not_running() {
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "Docker is installed but not running. Open the Docker Desktop app (Applications → Docker), wait until it" \
      "shows 'Engine running', then run this again."
  else
    echo "Docker is installed but not running, or this user may not use it. Start it with" \
      "'sudo systemctl start docker' (and 'sudo systemctl enable docker' to start it at boot). If it is running," \
      "add this user to the docker group with 'sudo usermod -aG docker \$USER', log out and back in, then run" \
      "this again. (With Docker Desktop on Linux: open the Docker Desktop app and wait for 'Engine running'.)"
  fi
}

# The compose network's addresses: the first /24 from a short list of uncommon private ranges that no Docker
# network on this machine overlaps, written into .env once (PISA_SUBNET, PISA_IP_RANGE, PISA_GATEWAY and
# PISA_PROXY_IP together; PISA_TRUSTED_PROXIES follows PISA_PROXY_IP in the compose file). A .env that already
# names a subnet keeps it.
ip_int() { local a b c d; IFS=. read -r a b c d <<< "$1"; echo $(( (a << 24) + (b << 16) + (c << 8) + d )); }

cidr_bounds() {  # "a.b.c.d/n" -> "first last"
  local ip="${1%/*}" bits="${1#*/}" start size
  size=$(( 1 << (32 - bits) ))
  start=$(( $(ip_int "$ip") & ~(size - 1) & 0xFFFFFFFF ))
  echo "$start $(( start + size - 1 ))"
}

docker_subnets() {
  local ids
  ids="$(docker network ls -q 2>/dev/null)" || return 0
  [ -n "$ids" ] || return 0
  # shellcheck disable=SC2086  # one id per word
  docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' $ids 2>/dev/null \
    | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' || true
}

FETCH_DIR="" ENV_COPY=""
cleanup() {  # on any exit: the fetched bundle's folder and a half-written copy of the .env
  [ -z "$FETCH_DIR" ] || rm -rf "$FETCH_DIR"
  [ -z "$ENV_COPY" ] || rm -f "$ENV_COPY"
}

NETWORK=pisa_default  # the compose project is `pisa` (docker-compose.yml's `name:`)

network_config() {  # "subnet ip_range gateway" of Pisa's existing compose network, or nothing
  docker network inspect "$NETWORK" \
    --format '{{range .IPAM.Config}}{{.Subnet}} {{.IPRange}} {{.Gateway}}{{end}}' 2>/dev/null || true
}

choose_subnet() {
  local env="$1" used candidate a b first last ufirst ulast clash subnet range gateway
  grep -q '^PISA_SUBNET=.' "$env" && return 0
  # An install from before this choice already has its network: keep it, never move a running install
  read -r subnet range gateway <<< "$(network_config)"
  if [[ "$subnet" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.0/24$ ]]; then
    b="${BASH_REMATCH[1]}"
    {
      echo ""
      echo "# The compose network this install already uses (kept by install.sh). Change all four together."
      echo "PISA_SUBNET=$subnet"
      echo "PISA_IP_RANGE=${range:-$b.128/25}"
      echo "PISA_GATEWAY=${gateway:-$b.1}"
      echo "PISA_PROXY_IP=$b.10"
    } >> "$env"
    say "Pisa's Docker network: $subnet (the one this install already uses)"
    return 0
  fi
  used="$(docker_subnets)"
  for candidate in 10.213.{0..15}.0 172.30.{0..15}.0; do
    read -r first last <<< "$(cidr_bounds "$candidate/24")"
    clash=0
    while read -r a; do
      [ -n "$a" ] || continue
      read -r ufirst ulast <<< "$(cidr_bounds "$a")"
      if [ "$first" -le "$ulast" ] && [ "$ufirst" -le "$last" ]; then clash=1; break; fi
    done <<< "$used"
    if [ "$clash" = 0 ]; then
      b="${candidate%.0}"
      {
        echo ""
        echo "# The compose network (chosen by install.sh: no other Docker network here overlaps it). Change all four together."
        echo "PISA_SUBNET=$b.0/24"
        echo "PISA_IP_RANGE=$b.128/25"
        echo "PISA_GATEWAY=$b.1"
        echo "PISA_PROXY_IP=$b.10"
      } >> "$env"
      say "Pisa's Docker network: $b.0/24"
      return 0
    fi
  done
  die "No free address range for Pisa's Docker network: every candidate (10.213.0-15.0/24, 172.30.0-15.0/24)
  overlaps a Docker network on this machine. Remove networks you no longer use ('docker network prune'), or set
  PISA_SUBNET, PISA_IP_RANGE, PISA_GATEWAY and PISA_PROXY_IP yourself in $env, then run this again."
}

AUTH_HEADER=""  # with PISA_GITHUB_TOKEN: a 0600 file holding the Authorization header (fetch_bundle makes it)

fetch() {  # fetch <url> <file> [<Accept header>]
  local -a headers=()
  [ -z "$AUTH_HEADER" ] || headers+=(--header "@$AUTH_HEADER")  # curl reads the header from the file
  [ -z "${3:-}" ] || headers+=(--header "Accept: $3")
  if command -v curl >/dev/null 2>&1; then
    # Redirected to another host (a release asset's storage), curl drops the Authorization header.
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 ${headers[@]+"${headers[@]}"} \
      --output "$2" "$1"
  elif [ -n "$AUTH_HEADER" ]; then
    die "PISA_GITHUB_TOKEN needs curl, which is not installed; install it, then run this again."
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

asset_id() {  # asset_id <release JSON> <name>: the id of the release's asset of that name, from the API's JSON
  grep -oE '"url"[[:space:]]*:[[:space:]]*"[^"]*/releases/assets/[0-9]+"|"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$1" \
    | awk -v want="$2" '
        /"url"/ { id = $0; sub(/.*\/releases\/assets\//, "", id); sub(/"$/, "", id); next }
        { name = $0; sub(/^"name"[[:space:]]*:[[:space:]]*"/, "", name); sub(/"$/, "", name)
          if (id != "" && name == want) { print id; exit } }'
}

fetch_asset() {  # fetch_asset <name> <file>: a release asset; through the API when PISA_GITHUB_TOKEN is set
  local api id
  if [ -z "$AUTH_HEADER" ]; then
    fetch "${PISA_RELEASE_BASE:-https://github.com}/$PISA_REPO/releases/download/$PISA_VERSION/$1" "$2"
    return
  fi
  api="${PISA_API_BASE:-https://api.github.com}/repos/$PISA_REPO/releases"
  if [ ! -f "$FETCH_DIR/.release/release.json" ]; then
    fetch "$api/tags/$PISA_VERSION" "$FETCH_DIR/.release/release.json" "application/vnd.github+json" || return 1
  fi
  id="$(asset_id "$FETCH_DIR/.release/release.json" "$1")" || id=""
  [ -n "$id" ] || return 1
  fetch "$api/assets/$id" "$2" "application/octet-stream"
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
  local home="$1" base path tmp
  [ -n "${PISA_REPO:-}" ] || die "set PISA_REPO to the repository the release comes from (owner/name), as the \
release notes' install lines do: PISA_REPO=<owner>/<repo> PISA_VERSION=${PISA_VERSION:-<tag>} bash install.sh"
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
  trap cleanup EXIT
  if [ -n "${PISA_GITHUB_TOKEN:-}" ]; then
    AUTH_HEADER="$tmp/.auth"  # umask 077: this user's alone; printf is a builtin, so no command line shows it
    printf 'Authorization: Bearer %s\n' "$PISA_GITHUB_TOKEN" > "$AUTH_HEADER"
    say "Using PISA_GITHUB_TOKEN for the downloads (a private repository)."
  fi
  for path in "${BUNDLE[@]}"; do
    mkdir -p "$tmp/$(dirname "$path")"
    fetch "$base/$path" "$tmp/$path" || die "could not fetch $path for $PISA_VERSION."
  done
  RELEASE_IMAGE=""
  if [[ "$PISA_VERSION" == v* ]]; then
    mkdir -p "$tmp/.release"
    fetch_asset SHA256SUMS "$tmp/.release/SHA256SUMS" \
      || die "could not fetch the release's SHA256SUMS for $PISA_VERSION; nothing was installed."
    fetch_asset IMAGES "$tmp/.release/IMAGES" \
      || die "could not fetch the release's IMAGES for $PISA_VERSION; nothing was installed."
    verify_bundle "$tmp" "$tmp/.release/SHA256SUMS"
    RELEASE_IMAGE="$(release_image "$tmp/.release/IMAGES")"
    say "Checked the bundle against the release's SHA256SUMS."
  fi
  rm -f "$tmp/.auth"
  AUTH_HEADER=""
  for path in "${BUNDLE[@]}"; do
    mkdir -p "$home/$(dirname "$path")"
    mv -f "$tmp/$path" "$home/$path"
  done
  chmod 0755 "$home/deploy/postgres/init/01-roles.sh"
  # the database, Tailscale and Caddy containers read them as their own users, with no capabilities
  chmod -R go+rX "$home/deploy/postgres" "$home/deploy/tailscale" "$home/deploy/caddy"
}

# This machine's time zone (an IANA name such as Europe/Rome), the default `pisa init` offers: inside the
# container the zone is the container's. Empty when it can't be told; init then asks with no default.
ZONE_FILE=/etc/timezone  # Debian's, and Raspberry Pi OS's
host_tz() {
  local tz="" link
  if [ "$(uname -s)" = "Darwin" ]; then
    link="$(readlink /etc/localtime 2>/dev/null || true)"  # /var/db/timezone/zoneinfo/Europe/Rome
    if [[ "$link" == *zoneinfo/* ]]; then
      tz="${link##*zoneinfo/}"
    else  # needs an administrator: usually refused, which the check below drops
      tz="$(systemsetup -gettimezone 2>/dev/null || true)"
      tz="${tz#Time Zone: }"
    fi
  else
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    if [ -z "$tz" ] && [ -r "$ZONE_FILE" ]; then
      tz="$(head -n 1 "$ZONE_FILE" 2>/dev/null || true)"
    fi
    if [ -z "$tz" ]; then
      link="$(readlink /etc/localtime 2>/dev/null || true)"  # ../usr/share/zoneinfo/Europe/Rome
      [[ "$link" != *zoneinfo/* ]] || tz="${link##*zoneinfo/}"
    fi
  fi
  tz="${tz#posix/}"
  tz="${tz#right/}"
  [[ "$tz" =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]] || tz=""  # init checks it again
  printf '%s' "$tz"
}

# Starting Pisa: `up -d`, then every START_POLL seconds a look at the services, until each one runs (healthy,
# where it has a health check) or exited cleanly (migrate, which runs once), for START_TIMEOUT seconds at most.
START_TIMEOUT=180
START_POLL=5

start_pisa() {  # start_pisa <deploy>: start the stack and wait until it is healthy; exits 1 if it isn't
  local deploy="$1" shown states pending waited=0 svc state health code skip=""
  local -a compose=(docker compose -f "$deploy/docker-compose.yml" --env-file "$deploy/.env")
  shown="docker compose -f $deploy/docker-compose.yml --env-file $deploy/.env"
  # tailscale mode without an auth key yet: Pisa runs, only the tailnet side waits for the key; don't wait on it
  if grep -q '^COMPOSE_PROFILES=.*tailscale' "$deploy/.env" && ! grep -q '^TS_AUTHKEY=.' "$deploy/.env"; then
    skip=" tailscale voice-relay "
  fi
  say "Starting Pisa (the first time, Docker downloads it first: a few minutes)."
  # The .env names another network than the one this install runs on: take the stack down first (volumes and
  # data stay), or compose keeps the old network for the running containers and new ones can't reach them
  local current wanted
  read -r current _ <<< "$(network_config)"
  wanted="$(sed -n 's/^PISA_SUBNET=//p' "$deploy/.env" | tail -1)"
  if [ -n "$current" ] && [ -n "$wanted" ] && [ "$current" != "$wanted" ]; then
    say "Pisa's network changes from $current to $wanted: stopping it first (your data stays)."
    "${compose[@]}" down || die "Could not stop Pisa to change its network. Run: $shown down, then this again."
  fi
  if ! "${compose[@]}" up -d; then
    die "Pisa did not start (the messages above say why). If pulling the images failed with 'input/output
  error', quit Docker Desktop (whale menu > Quit Docker Desktop), open it again, wait for 'Engine running',
  and run this again. To see what a service says: $shown logs --tail 50 <service>"
  fi
  while :; do
    pending=""
    states="$("${compose[@]}" ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null)" \
      || states=""
    while IFS='|' read -r svc state health code; do
      [ -n "$svc" ] || continue
      [[ "$skip" != *" $svc "* ]] || continue
      case "$state" in
        running) [ -z "$health" ] || [ "$health" = healthy ] || pending+=" $svc($health)" ;;
        exited) [ "$code" = 0 ] || pending+=" $svc(exited $code)" ;;
        *) pending+=" $svc($state)" ;;
      esac
    done <<< "$states"
    if [ -n "$states" ] && [ -z "$pending" ]; then
      if [ -n "$skip" ]; then
        say "Tailscale has no auth key yet: Pisa runs, but your phone can't reach it until you add one. Add it"
        say "later (make a key at https://login.tailscale.com/admin/settings/keys; paste it when 'read' waits):"
        say "  read -rs k; printf 'TS_AUTHKEY=%s\\n' \"\$k\" >> $deploy/.env; unset k"
        say "  $shown up -d"
      fi
      return 0
    fi
    if [ "$waited" -ge "$START_TIMEOUT" ]; then
      [ -n "$pending" ] || pending=" (no service is running)"
      printf 'error: Pisa did not become healthy within %s seconds. Not ready:%s
' "$START_TIMEOUT" "$pending" >&2
      for svc in $pending; do
        svc="${svc%%(*}"
        [ -z "$svc" ] || printf '  to see what %s says: %s logs --tail 50 %s
' "$svc" "$shown" "$svc" >&2
      done
      printf 'Once it is fixed, start it with: %s up -d
' "$shown" >&2
      exit 1
    fi
    say "  waiting for${pending:- the services to appear} (${waited}s of ${START_TIMEOUT}s)"
    sleep "$START_POLL"
    waited=$(( waited + START_POLL ))
  done
}

# A re-run with a new PISA_VERSION on a set-up install is its update: the .env's PISA_IMAGE line takes the
# release's image by digest (from its IMAGES); every other line stays exactly as it was. The file is rewritten
# in place, so its owner and mode stay too.
set_image() {  # set_image <env> <ref>
  local env="$1" ref="$2" tmp
  if grep -qxF "PISA_IMAGE=$ref" "$env"; then
    say "Pisa's image is already $PISA_VERSION's."
    return 0
  fi
  tmp="$(mktemp)"
  chmod 0600 "$tmp"  # a copy of the .env, passwords included: this user only, and removed on any exit
  ENV_COPY="$tmp"
  trap cleanup EXIT
  if grep -q '^PISA_IMAGE=' "$env"; then
    awk -v line="PISA_IMAGE=$ref" '/^PISA_IMAGE=/ { print line; next } { print }' "$env" > "$tmp"
  else
    cat "$env" > "$tmp"
    [ ! -s "$tmp" ] || [ -z "$(tail -c 1 "$tmp")" ] || echo "" >> "$tmp"  # its last line ends first
    printf 'PISA_IMAGE=%s\n' "$ref" >> "$tmp"
  fi
  cat "$tmp" > "$env"
  rm -f "$tmp"
  ENV_COPY=""
  say "Updated to $PISA_VERSION: PISA_IMAGE in $env is now $ref (nothing else in it changed)."
}

running_steps() {  # running_steps <deploy>: what to do once Pisa runs
  local shown="docker compose -f $1/docker-compose.yml --env-file $1/.env"
  say "Pisa is running."
  say ""
  say "Next:"
  say "  $shown exec worker pisa setup    # your AI provider key, notifications"
  say "  $shown exec worker pisa pair     # the QR code your phone scans"
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

  local here home deploy image="" RELEASE_IMAGE=""
  # piped (curl ... | bash) there is no script file: never a checkout, and init asks on the terminal
  local script="${BASH_SOURCE[0]:-}" ask=/dev/stdin
  here=""
  [ -z "$script" ] || [ ! -f "$script" ] || here="$(cd "$(dirname "$script")" && pwd)"
  if [ -z "$script" ] && [ ! -t 0 ]; then
    ask=/dev/null  # the pipe holds the script, not answers: --yes or the terminal
    if { : </dev/tty; } 2>/dev/null; then ask=/dev/tty; fi
  fi
  if [ -n "$here" ] && [ -f "$here/docker-compose.yml" ] && [ -d "$here/../core" ]; then
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
    unset PISA_GITHUB_TOKEN  # fetched: nothing after this (docker, pisa init) sees it
    image="${PISA_IMAGE:-$RELEASE_IMAGE}"
    [[ "$image" == *@sha256:* ]] || say "Note: PISA_IMAGE names a tag, not a digest: what it runs can change."
    say "Installed the compose bundle in $home"
  fi
  deploy="$home/deploy"
  tls_dir "$deploy"
  [ -e "$deploy/.env" ] || : > "$deploy/.env"  # compose wants the file; `pisa init` fills it
  chmod 0600 "$deploy/.env"
  choose_subnet "$deploy/.env"  # before any compose command: the first one creates the network

  # --no-start is the script's; every other flag is init's
  local start=1 arg
  local -a flags=()
  for arg in "$@"; do
    if [ "$arg" = --no-start ]; then start=0; else flags+=("$arg"); fi
  done
  set -- ${flags[@]+"${flags[@]}"}

  # Run again on an install that is already set up (its .env holds the database password): keep the .env as it
  # is and start it, unless `--force` (or `--rotate-tls`) asks init to update it. A release's run (a new
  # PISA_VERSION) is the update: only the .env's PISA_IMAGE changes, to that release's image.
  if grep -q '^POSTGRES_PASSWORD=.' "$deploy/.env" && [[ " $* " != *" --force "* && " $* " != *" --rotate-tls "* ]]; then
    say "Pisa is already set up in $deploy (its .env is kept: passwords and keys unchanged)."
    [ -z "$RELEASE_IMAGE" ] || set_image "$deploy/.env" "$RELEASE_IMAGE"
    say "To change its settings, run this again with --force (passwords and keys are still kept)."
    if [ "$start" = 1 ]; then
      start_pisa "$deploy"
      running_steps "$deploy"
    else
      say "Start it with: docker compose -f $deploy/docker-compose.yml --env-file $deploy/.env up -d"
    fi
    return 0
  fi

  local -a init_args=(--env-file /setup/.env --show-dir "$deploy")
  [ "$start" = 0 ] || init_args+=(--started)  # this script starts it and prints the steps after
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
  local zone
  zone="$(host_tz)"
  [ -z "$zone" ] || run_args+=(--env "PISA_HOST_TZ=$zone")  # the time zone question's default

  # Placeholders for the compose file's required passwords, for this one command only: `pisa init` makes the
  # real ones, and nothing here starts the database.
  POSTGRES_PASSWORD=init PISA_MIGRATE_PASSWORD=init PISA_APP_PASSWORD=init PISA_IMAGE="${image:-${PISA_IMAGE:-}}" \
    docker compose -f "$deploy/docker-compose.yml" --env-file "$deploy/.env" \
    run "${run_args[@]}" api pisa init "${init_args[@]}" "$@" <"$ask"
  if [ "$start" = 1 ]; then
    start_pisa "$deploy"
    running_steps "$deploy"
  fi
}

# Run, unless sourced (the release workflow reads BUNDLE from this file).
if ! (return 0 2>/dev/null); then
  main "$@"
fi
