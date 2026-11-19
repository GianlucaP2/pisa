# SPDX-License-Identifier: AGPL-3.0-only
# Install Pisa on Windows with Docker Desktop: a folder with the compose bundle, then `pisa init` run inside the
# Pisa image (Windows needs no Python), which writes deploy\.env.
#
# It installs nothing else: without Docker it says where to get it and stops. Download it, read it, then run
# in PowerShell:
#
#   $env:PISA_REPO = '<owner>/<repo>'; $env:PISA_VERSION = '<tag or commit>'; .\install.ps1 [pisa init flags...]
#   .\deploy\install.ps1 [pisa init flags...]          # from a checkout: uses it in place, builds the image
#
#   PISA_REPO      the GitHub repository the release comes from, owner/name (no default)
#   PISA_VERSION   a release tag (v1.2.3) or a full commit id: nothing is fetched from a moving branch
#   PISA_HOME      the folder to install into (default: $HOME\pisa): a new or empty folder, or an earlier install
#   PISA_REGISTRY  the image registry (default: the release's, ghcr.io); PISA_IMAGE overrides the whole image
#   PISA_RAW_BASE  where the bundle's files are fetched from (default: https://raw.githubusercontent.com)
#   PISA_RELEASE_BASE  where the release's assets are fetched from (default: https://github.com)
#   TS_AUTHKEY     tailscale mode: the auth key, read from the environment rather than a prompt
#
# Arguments go to `pisa init`: --mode tailscale|caddy|local, --domain, --email, --timezone, --yes, --force.
#
# A release tag's bundle is checked against the release's SHA256SUMS (every file, or nothing is installed), and
# the image is written into .env by digest, from the release's IMAGES: ghcr.io/<owner>/pisa@sha256:<digest>,
# never a tag that can be moved. A commit id names its files' content itself; it has no release, so it needs
# PISA_IMAGE set to an image by digest (name@sha256:<digest>).
#
# The install folder is made this user's alone (and SYSTEM's, for Docker Desktop), everything in it included:
# it holds the .env's passwords, the local mode's TLS key and the compose file Docker runs. A folder of its own
# under C:\ would otherwise let every local user read the key and edit the compose file.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$DockerLink = 'https://docs.docker.com/get-started/get-docker/'
# The bundle, as paths in the repository; each lands at the same path under PISA_HOME.
$Bundle = @('deploy/docker-compose.yml', 'deploy/postgres/init/01-roles.sh', 'deploy/tailscale/serve.json',
    'deploy/caddy/Caddyfile', '.env.example')
$Bundle += @('deploy/.env.example')  # read by people, not by compose
$Bundle += @('deploy/tailscale/cert-export.sh')  # the tailscale sidecar's entrypoint
$ImageRe = '^[a-z0-9][a-z0-9.:-]*(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$'

function Fail([string]$Message) {
    [Console]::Error.WriteLine("error: $Message")
    exit 1
}

function Test-Platform {
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }  # 32-bit PowerShell on 64-bit Windows
    if ($arch -notin @('AMD64', 'ARM64')) {
        Fail "Pisa's images are 64-bit only (amd64, arm64); this Windows reports $arch."
    }
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "Docker is not installed. Install Docker Desktop from $DockerLink, then run this again."
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 (docker compose) is missing. See $DockerLink, then run this again." }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail 'Docker is installed but not running. Start Docker Desktop, then run this again.' }
}

function Get-Fetched([string]$Uri, [string]$OutFile, [string]$What) {
    try { Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile }
    catch { Fail "could not fetch $What for $($env:PISA_VERSION); nothing was installed." }
}

function Test-BundleSums([string]$Dir, [string]$SumsFile) {
    # Every bundle file has the hash the release's SHA256SUMS lists (as sha256sum writes it: "<hex>  <path>").
    $sums = @{}
    foreach ($line in (Get-Content -LiteralPath $SumsFile)) {
        if ($line -cmatch '^([0-9a-f]{64}) [ *](.+)$') { $sums[$Matches[2]] = $Matches[1] }
    }
    foreach ($path in $Bundle) {
        if (-not $sums.ContainsKey($path)) { Fail "the release's SHA256SUMS does not list $path; nothing was installed." }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Dir $path)).Hash.ToLowerInvariant()
        if ($got -cne $sums[$path]) {
            Fail "$path does not match the release's SHA256SUMS (it may have been changed); nothing was installed."
        }
    }
}

function Get-ReleaseImage([string]$ImagesFile) {
    # The server image by digest, as the release's IMAGES lists it ("pisa <name>@sha256:<digest>").
    $ref = ''
    foreach ($line in (Get-Content -LiteralPath $ImagesFile)) {
        $fields = -split $line
        if ($fields.Count -eq 2 -and $fields[0] -ceq 'pisa') { $ref = $fields[1]; break }
    }
    if ($ref -cnotmatch $ImageRe) { Fail "the release's IMAGES has no server image by digest; nothing was installed." }
    if ($env:PISA_REGISTRY) { $ref = "$($env:PISA_REGISTRY)/" + $ref.Substring($ref.IndexOf('/') + 1) }  # a mirror
    return $ref
}

function Get-Bundle([string]$Home_) {
    # The bundle, checked; returns the release's image by digest for a release tag (empty for a commit).
    if (-not $env:PISA_REPO) { Fail 'set $env:PISA_REPO to the repository the release comes from (owner/name).' }
    if ($env:PISA_REPO -notmatch '^[^/\s]+/[^/\s]+$') { Fail "PISA_REPO must be owner/name, not '$($env:PISA_REPO)'." }
    $version = $env:PISA_VERSION
    if (-not $version) { Fail 'set $env:PISA_VERSION to a release tag (v1.2.3) or a full commit id.' }
    if ($version -cnotmatch '^v\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$' -and $version -cnotmatch '^[0-9a-f]{40}$') {
        Fail "PISA_VERSION '$version' is not pinned: use a release tag (v1.2.3) or a full 40-character commit id."
    }
    $rawBase = if ($env:PISA_RAW_BASE) { $env:PISA_RAW_BASE } else { 'https://raw.githubusercontent.com' }
    if (-not $rawBase.StartsWith('https://')) { Fail 'PISA_RAW_BASE must be an https:// URL.' }
    $base = "$rawBase/$($env:PISA_REPO)/$version"
    $releaseBase = if ($env:PISA_RELEASE_BASE) { $env:PISA_RELEASE_BASE } else { 'https://github.com' }
    if (-not $releaseBase.StartsWith('https://')) { Fail 'PISA_RELEASE_BASE must be an https:// URL.' }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("pisa-" + [Guid]::NewGuid().ToString('N'))
    $releaseImage = ''
    try {
        foreach ($path in $Bundle) {
            $file = Join-Path $tmp $path
            New-Item -ItemType Directory -Force -Path (Split-Path $file) | Out-Null
            try { Invoke-WebRequest -UseBasicParsing -Uri "$base/$path" -OutFile $file }
            catch { Fail "could not fetch $path for $version." }
        }
        if ($version.StartsWith('v')) {
            $release = "$releaseBase/$($env:PISA_REPO)/releases/download/$version"
            $sumsFile = Join-Path $tmp 'SHA256SUMS.release'
            $imagesFile = Join-Path $tmp 'IMAGES.release'
            Get-Fetched "$release/SHA256SUMS" $sumsFile "the release's SHA256SUMS"
            Get-Fetched "$release/IMAGES" $imagesFile "the release's IMAGES"
            Test-BundleSums $tmp $sumsFile
            $releaseImage = Get-ReleaseImage $imagesFile
            Write-Output "Checked the bundle against the release's SHA256SUMS." | Out-Host
        }
        foreach ($path in $Bundle) {
            $target = Join-Path $Home_ $path
            New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
            Move-Item -Force -Path (Join-Path $tmp $path) -Destination $target
        }
    } finally {
        Remove-Item -Recurse -Force -Path $tmp -ErrorAction SilentlyContinue
    }
    return $releaseImage
}

function Test-Home([string]$Path) {
    # The folder Protect-Tree takes over: never a drive's root or the profile itself, and never a folder that
    # holds anything but an earlier install.
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\', '/')
    if ($full -eq $root -or $full -eq [IO.Path]::GetFullPath($HOME).TrimEnd('\', '/')) {
        Fail "PISA_HOME must be a folder of its own (e.g. $HOME\pisa or D:\pisa), not '$Path'."
    }
    if ((Test-Path -LiteralPath $full) -and (Get-ChildItem -LiteralPath $full -Force | Select-Object -First 1) -and
        -not (Test-Path -LiteralPath (Join-Path $full 'deploy\docker-compose.yml'))) {
        Fail "PISA_HOME '$Path' already holds other files: choose a new or empty folder."
    }
}

function Protect-Tree([string]$Path) {
    # This user and SYSTEM (Docker Desktop's service) only, on the folder and, inherited, on everything in it
    # now and later (E1-PACK PK-3): the .env, the local mode's TLS key and the compose file Docker runs.
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r "*${me}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "could not make $Path yours alone (icacls)." }
    if (Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1) {
        & icacls.exe (Join-Path $Path '*') /reset /T /C /Q | Out-Null  # what is in it inherits the folder's rule
        if ($LASTEXITCODE -ne 0) { Fail "could not make the files in $Path yours alone (icacls)." }
    }
}

function Protect-File([string]$Path) {
    # The .env holds passwords: readable by this user only.
    $acl = Get-Acl $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')))
    Set-Acl -Path $Path -AclObject $acl
}

Test-Platform
Test-Docker

$image = ''
$here = $PSScriptRoot
if ((Test-Path (Join-Path $here 'docker-compose.yml')) -and (Test-Path (Join-Path $here '../core'))) {
    $pisaHome = (Resolve-Path (Join-Path $here '..')).Path  # a checkout: use it in place, the image is built from core/
    Write-Output "Using this checkout: $pisaHome"
} else {
    $pisaHome = if ($env:PISA_HOME) { $env:PISA_HOME } else { Join-Path $HOME 'pisa' }
    if (-not $env:PISA_IMAGE -and "$($env:PISA_VERSION)" -cmatch '^[0-9a-f]{40}$') {
        Fail 'a commit id has no release to take the image from: set $env:PISA_IMAGE to one by digest (name@sha256:...).'
    }
    Test-Home $pisaHome
    New-Item -ItemType Directory -Force -Path $pisaHome | Out-Null
    $pisaHome = (Resolve-Path $pisaHome).Path
    Protect-Tree $pisaHome  # before anything lands in it
    $releaseImage = Get-Bundle $pisaHome
    $image = if ($env:PISA_IMAGE) { $env:PISA_IMAGE } else { $releaseImage }
    if (-not $image.Contains('@sha256:')) { Write-Output 'Note: PISA_IMAGE names a tag, not a digest: what it runs can change.' }
    Write-Output "Installed the compose bundle in $pisaHome"
}
$deploy = Join-Path $pisaHome 'deploy'
# the local mode's certificate and key; Docker Desktop maps ownership on Windows, so no --user is needed
New-Item -ItemType Directory -Force -Path (Join-Path $deploy 'tls') | Out-Null
if ($image) { Protect-Tree $pisaHome } else { Protect-Tree (Join-Path $deploy 'tls') }  # a checkout: its key only
$envFile = Join-Path $deploy '.env'
if (-not (Test-Path $envFile)) { New-Item -ItemType File -Path $envFile | Out-Null }  # compose wants it; init fills it
Protect-File $envFile

$initArgs = @('--env-file', '/setup/.env', '--show-dir', $deploy)
if ($image) { $initArgs += @('--image', $image) }

# Placeholders for the compose file's required passwords, for this one command only: `pisa init` makes the real
# ones, and nothing here starts the database.
$saved = @{}
$stand_in = 'init'
$placeholders = @{ POSTGRES_PASSWORD = $stand_in; PISA_MIGRATE_PASSWORD = $stand_in; PISA_APP_PASSWORD = $stand_in; PISA_IMAGE = $image }
foreach ($name in $placeholders.Keys) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($name -ne 'PISA_IMAGE' -or $image) { [Environment]::SetEnvironmentVariable($name, $placeholders[$name], 'Process') }
}
try {
    & docker compose -f (Join-Path $deploy 'docker-compose.yml') --env-file $envFile `
        run --rm --no-deps --volume "${deploy}:/setup" --env TS_AUTHKEY --env "PISA_HOST_NAME=$($env:COMPUTERNAME)" `
        api pisa init @initArgs @args
    $code = $LASTEXITCODE
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
if (Test-Path $envFile) { Protect-File $envFile }  # init replaced the file; it took the folder's permissions
exit $code
