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
#   PISA_GITHUB_TOKEN  optional: a GitHub token, to install a release of a private repository. The bundle is then
#                  fetched with it (an Authorization header, Invoke-WebRequest -Headers), and the release's assets
#                  through the GitHub API (PISA_API_BASE, default https://api.github.com), since a private release's
#                  download links take no token. It is never printed or written to .env, and is removed from this
#                  session's environment once the bundle is fetched. Don't run this under Start-Transcript.
#                  Without it, nothing changes.
#   TS_AUTHKEY     tailscale mode: the auth key, read from the environment rather than a prompt
#
# Arguments go to `pisa init`: --mode tailscale|caddy|local, --domain, --email, --timezone, --country, --yes,
# --force; except one, the script's own:
#   --no-start     write the .env only; don't start Pisa (by default the script runs `docker compose up -d` once
#                  init has written it, waits until every service is healthy, then prints the next steps)
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
    if ($LASTEXITCODE -ne 0) { Fail "Docker is installed but not running. Open Docker Desktop (Start menu → Docker Desktop), wait until it shows 'Engine running', then run this again." }
}

# With PISA_GITHUB_TOKEN: the Authorization header every download carries (a redirect to another host drops it).
$AuthHeaders = @{}
if ($env:PISA_GITHUB_TOKEN) { $AuthHeaders['Authorization'] = "Bearer $($env:PISA_GITHUB_TOKEN)" }

function Get-Fetched([string]$Uri, [string]$OutFile, [string]$What) {
    try { Invoke-WebRequest -UseBasicParsing -Headers $AuthHeaders -Uri $Uri -OutFile $OutFile }
    catch { Fail "could not fetch $What for $($env:PISA_VERSION); nothing was installed." }
}

function Get-ReleaseAsset([string]$Name, [string]$OutFile, [string]$ReleaseBase, [string]$ApiBase) {
    # A release asset: its download link, or, with PISA_GITHUB_TOKEN, through the API (a private release).
    $version = $env:PISA_VERSION
    if (-not $AuthHeaders.Count) {
        Get-Fetched "$ReleaseBase/$($env:PISA_REPO)/releases/download/$version/$Name" $OutFile "the release's $Name"
        return
    }
    $api = "$ApiBase/repos/$($env:PISA_REPO)/releases"
    try {
        $info = Invoke-RestMethod -UseBasicParsing -Headers ($AuthHeaders + @{ Accept = 'application/vnd.github+json' }) `
            -Uri "$api/tags/$version"
    } catch { Fail "could not fetch the release $version through the GitHub API; nothing was installed." }
    $asset = @($info.assets | Where-Object { $_.name -ceq $Name }) | Select-Object -First 1
    if (-not $asset) { Fail "the release $version has no $Name; nothing was installed." }
    try {
        Invoke-WebRequest -UseBasicParsing -Headers ($AuthHeaders + @{ Accept = 'application/octet-stream' }) `
            -Uri "$api/assets/$([long]$asset.id)" -OutFile $OutFile
    } catch { Fail "could not fetch the release's $Name for $version; nothing was installed." }
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
    $apiBase = if ($env:PISA_API_BASE) { $env:PISA_API_BASE } else { 'https://api.github.com' }
    if (-not $apiBase.StartsWith('https://')) { Fail 'PISA_API_BASE must be an https:// URL.' }
    if ($AuthHeaders.Count) { Write-Output 'Using PISA_GITHUB_TOKEN for the downloads (a private repository).' | Out-Host }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("pisa-" + [Guid]::NewGuid().ToString('N'))
    $releaseImage = ''
    try {
        foreach ($path in $Bundle) {
            $file = Join-Path $tmp $path
            New-Item -ItemType Directory -Force -Path (Split-Path $file) | Out-Null
            try { Invoke-WebRequest -UseBasicParsing -Headers $AuthHeaders -Uri "$base/$path" -OutFile $file }
            catch { Fail "could not fetch $path for $version." }
        }
        if ($version.StartsWith('v')) {
            $sumsFile = Join-Path $tmp 'SHA256SUMS.release'
            $imagesFile = Join-Path $tmp 'IMAGES.release'
            Get-ReleaseAsset 'SHA256SUMS' $sumsFile $releaseBase $apiBase
            Get-ReleaseAsset 'IMAGES' $imagesFile $releaseBase $apiBase
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

function ConvertTo-Range([string]$Cidr) {
    # "a.b.c.d/n" -> @(first, last) as unsigned integers
    $ip, $bits = $Cidr.Split('/')
    $b = ([Net.IPAddress]::Parse($ip)).GetAddressBytes()
    $n = ([uint64]$b[0] -shl 24) + ([uint64]$b[1] -shl 16) + ([uint64]$b[2] -shl 8) + [uint64]$b[3]
    $size = [uint64]1 -shl (32 - [int]$bits)
    $first = $n - ($n % $size)
    return @($first, ($first + $size - 1))
}

function Set-NetworkRange([string]$EnvFile) {
    # The compose network's addresses: the first /24 from a short list of uncommon private ranges that no Docker
    # network here overlaps, written into .env once, all four together. A .env that already names one keeps it.
    if (Select-String -Path $EnvFile -Pattern '^PISA_SUBNET=.' -Quiet) { return }
    $used = @()
    $ids = @(docker network ls -q 2>$null)
    if ($ids.Count -gt 0) {
        $used = @(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' @ids 2>$null) -split '\s+' |
            Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+/\d+$' }
    }
    $candidates = @(0..15 | ForEach-Object { "10.213.$_" }) + @(0..15 | ForEach-Object { "172.30.$_" })
    foreach ($base in $candidates) {
        $r = ConvertTo-Range "$base.0/24"
        $clash = $false
        foreach ($u in $used) {
            $o = ConvertTo-Range $u
            if ($r[0] -le $o[1] -and $o[0] -le $r[1]) { $clash = $true; break }
        }
        if (-not $clash) {
            Add-Content -Path $EnvFile -Value @(
                '',
                '# The compose network (chosen by install.ps1: no other Docker network here overlaps it). Change all four together.',
                "PISA_SUBNET=$base.0/24", "PISA_IP_RANGE=$base.128/25", "PISA_GATEWAY=$base.1", "PISA_PROXY_IP=$base.10")
            Write-Host "Pisa's Docker network: $base.0/24"
            return
        }
    }
    Fail ("No free address range for Pisa's Docker network: every candidate (10.213.0-15.0/24, 172.30.0-15.0/24) " +
        "overlaps a Docker network here. Remove networks you no longer use ('docker network prune'), or set " +
        "PISA_SUBNET, PISA_IP_RANGE, PISA_GATEWAY and PISA_PROXY_IP yourself in $EnvFile, then run this again.")
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
    # now and later: the .env, the local mode's TLS key and the compose file Docker runs.
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
    # fetched: nothing after this (docker, pisa init) sees the token
    $AuthHeaders = @{}
    Remove-Item Env:PISA_GITHUB_TOKEN -ErrorAction SilentlyContinue
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
Set-NetworkRange $envFile  # before any compose command: the first one creates the network

# Starting Pisa: `up -d`, then every $StartPoll seconds a look at the services, until each one runs (healthy,
# where it has a health check) or exited cleanly (migrate, which runs once), for $StartTimeout seconds at most.
$StartTimeout = 180
$StartPoll = 5
function Start-Pisa([string]$Deploy) {
    $composeFile = Join-Path $Deploy 'docker-compose.yml'
    $envPath = Join-Path $Deploy '.env'
    $shown = "docker compose -f $composeFile --env-file $envPath"
    Write-Host 'Starting Pisa (the first time, Docker downloads it first: a few minutes).'
    & docker compose -f $composeFile --env-file $envPath up -d
    if ($LASTEXITCODE -ne 0) {
        Fail ("Pisa did not start (the messages above say why). If pulling the images failed with 'input/output " +
            "error', quit Docker Desktop (whale menu > Quit Docker Desktop), open it again, wait for 'Engine " +
            "running', and run this again. To see what a service says: $shown logs --tail 50 <service>")
    }
    # tailscale mode without an auth key yet: Pisa runs, only the tailnet side waits for the key; don't wait on it
    $skip = @()
    if ((Select-String -Path $envPath -Pattern '^COMPOSE_PROFILES=.*tailscale' -Quiet) -and
        -not (Select-String -Path $envPath -Pattern '^TS_AUTHKEY=.' -Quiet)) { $skip = @('tailscale', 'voice-relay') }
    $waited = 0
    while ($true) {
        $lines = @(& docker compose -f $composeFile --env-file $envPath ps -a `
            --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>$null)
        $pending = @()
        $seen = $false
        foreach ($line in $lines) {
            $f = "$line".Split('|')
            if ($f.Count -lt 4 -or -not $f[0]) { continue }
            $seen = $true
            $svc, $state, $health, $exitCode = $f[0], $f[1], $f[2], $f[3]
            if ($skip -contains $svc) { continue }
            if ($state -eq 'running') {
                if ($health -and $health -ne 'healthy') { $pending += "$svc($health)" }
            } elseif ($state -eq 'exited') {
                if ($exitCode -ne '0') { $pending += "$svc(exited $exitCode)" }
            } else {
                $pending += "$svc($state)"
            }
        }
        if ($seen -and $pending.Count -eq 0) {
            if ($skip.Count -gt 0) {
                Write-Host "Tailscale has no auth key yet: Pisa runs, but your phone can't reach it until you add one."
                Write-Host 'Add it later (make a key at https://login.tailscale.com/admin/settings/keys):'
                Write-Host "  `$k = Read-Host -AsSecureString 'Key'; Add-Content -Path '$envPath' -Value ('TS_AUTHKEY=' + [Net.NetworkCredential]::new('', `$k).Password)"
                Write-Host "  $shown up -d"
            }
            return
        }
        if ($waited -ge $StartTimeout) {
            if ($pending.Count -eq 0) { $pending = @('(no service is running)') }
            [Console]::Error.WriteLine(
                "error: Pisa did not become healthy within $StartTimeout seconds. Not ready: $($pending -join ' ')")
            foreach ($item in $pending) {
                $svc = $item.Split('(')[0]
                if ($svc) { [Console]::Error.WriteLine("  to see what $svc says: $shown logs --tail 50 $svc") }
            }
            [Console]::Error.WriteLine("Once it is fixed, start it with: $shown up -d")
            exit 1
        }
        $what = if ($pending.Count -gt 0) { ' ' + ($pending -join ' ') } else { ' the services to appear' }
        Write-Host "  waiting for$what (${waited}s of ${StartTimeout}s)"
        Start-Sleep -Seconds $StartPoll
        $waited += $StartPoll
    }
}
function Show-RunningSteps([string]$Deploy) {
    $shown = "docker compose -f $(Join-Path $Deploy 'docker-compose.yml') --env-file $(Join-Path $Deploy '.env')"
    Write-Host 'Pisa is running.'
    Write-Host ''
    Write-Host 'Next:'
    Write-Host "  $shown exec worker pisa setup    # your AI provider key, notifications"
    Write-Host "  $shown exec worker pisa pair     # the QR code your phone scans"
}

# --no-start is the script's; every other argument is init's
$start = -not ($args -contains '--no-start')
$initFlags = @($args | Where-Object { $_ -ne '--no-start' })

# Run again on an install that is already set up: keep its .env and start it, unless -- --force.
if ((Select-String -Path $envFile -Pattern '^POSTGRES_PASSWORD=.' -Quiet) -and
    -not ($initFlags -contains '--force' -or $initFlags -contains '--rotate-tls')) {
    Write-Host "Pisa is already set up in $deploy (its .env is kept: passwords and keys unchanged)."
    Write-Host 'To change its settings, run this again with --force (passwords and keys are still kept).'
    if ($start) {
        Start-Pisa $deploy
        Show-RunningSteps $deploy
    } else {
        Write-Host "Start it with: docker compose -f $(Join-Path $deploy 'docker-compose.yml') --env-file $envFile up -d"
    }
    exit 0
}

$initArgs = @('--env-file', '/setup/.env', '--show-dir', $deploy)
if ($image) { $initArgs += @('--image', $image) }
if ($start) { $initArgs += @('--started') }  # this script starts it and prints the steps after

# Placeholders for the compose file's required passwords, for this one command only: `pisa init` makes the real
# ones, and nothing here starts the database.
$saved = @{}
$stand_in = 'init'
$placeholders = @{ POSTGRES_PASSWORD = $stand_in; PISA_MIGRATE_PASSWORD = $stand_in; PISA_APP_PASSWORD = $stand_in; PISA_IMAGE = $image }
foreach ($name in $placeholders.Keys) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($name -ne 'PISA_IMAGE' -or $image) { [Environment]::SetEnvironmentVariable($name, $placeholders[$name], 'Process') }
}
# This machine's time zone as an IANA name (Europe/Rome), the default `pisa init` offers: the container has its
# own. Windows names zones its own way; .NET 6+ (PowerShell 7) converts them. Without it, nothing is passed and
# init asks with no default.
$zoneArgs = @()
try {
    $iana = $null
    if ([System.TimeZoneInfo]::TryConvertWindowsIdToIanaId((Get-TimeZone).Id, [ref]$iana) -and $iana) {
        $zoneArgs = @('--env', "PISA_HOST_TZ=$iana")
    }
} catch { $zoneArgs = @() }  # Windows PowerShell 5.1 (.NET Framework) has no such method
try {
    & docker compose -f (Join-Path $deploy 'docker-compose.yml') --env-file $envFile `
        run --rm --no-deps --volume "${deploy}:/setup" --env TS_AUTHKEY --env "PISA_HOST_NAME=$($env:COMPUTERNAME)" `
        @zoneArgs api pisa init @initArgs @initFlags
    $code = $LASTEXITCODE
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
if (Test-Path $envFile) { Protect-File $envFile }  # init replaced the file; it took the folder's permissions
if ($code -eq 0 -and $start) {
    Start-Pisa $deploy
    Show-RunningSteps $deploy
}
exit $code
