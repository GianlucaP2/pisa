# SPDX-License-Identifier: AGPL-3.0-only
"""deploy/install.sh checks a release's bundle against its SHA256SUMS and takes the image by digest from its
IMAGES, with the downloads replaced by local copies: a changed or unlisted file installs
nothing, and an image without a digest is refused. install.ps1 fetches the same files. Synthetic names
only."""

# ruff: noqa: S603, S607 (bash runs the repository's own install script)

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.fake_secrets import tailscale_key

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "install.sh"
DIGEST = "sha256:" + "ab" * 32
IMAGE = f"ghcr.io/example-owner/pisa@{DIGEST}"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# fetch() replaced: a URL under the raw or release base is served from SRC.
HARNESS = r"""
source "$SCRIPT"
fetch() {
  local u="$1"
  u="${u#https://raw.invalid/example-owner/pisa/v1.2.3/}"
  u="${u#https://release.invalid/example-owner/pisa/releases/download/v1.2.3/}"
  [ -f "$SRC/$u" ] || return 1
  cp "$SRC/$u" "$2"
}
fetch_bundle "$HOME_DIR"
printf 'image=%s\n' "$RELEASE_IMAGE"
"""


def bundle() -> list[str]:
    out = subprocess.run(
        ["bash", "-c", 'source "$0" && printf "%s\\n" "${BUNDLE[@]}"', str(SCRIPT)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.split()


def release(tmp_path: Path) -> Path:
    """The release as published: the bundle's files, SHA256SUMS (as `sha256sum` writes it) and IMAGES."""
    src = tmp_path / "src"
    sums = []
    for path in [*bundle(), "deploy/install.sh", "deploy/install.ps1"]:
        (src / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, src / path)
        sums.append(f"{hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}  {path}\n")
    (src / "SHA256SUMS").write_text("".join(sums))
    (src / "IMAGES").write_text(f"pisa {IMAGE}\npisa-relay ghcr.io/example-owner/pisa-relay@{DIGEST}\n")
    return src


def install(tmp_path: Path, src: Path, **env: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environ = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "SCRIPT": str(SCRIPT), "SRC": str(src), "HOME_DIR": str(home),
        "PISA_REPO": "example-owner/pisa", "PISA_VERSION": "v1.2.3", "PISA_RAW_BASE": "https://raw.invalid",
        "PISA_RELEASE_BASE": "https://release.invalid", **env,
    }  # fmt: skip
    return subprocess.run(["bash", "-c", HARNESS], capture_output=True, text=True, env=environ, timeout=60)


def test_a_release_bundle_that_matches_installs_and_gives_the_image_by_digest(tmp_path: Path) -> None:
    done = install(tmp_path, release(tmp_path))
    assert done.returncode == 0, done.stderr
    assert f"image={IMAGE}" in done.stdout
    for path in bundle():
        assert (tmp_path / "home" / path).read_bytes() == (ROOT / path).read_bytes()


def test_a_mirror_registry_keeps_the_digest(tmp_path: Path) -> None:
    done = install(tmp_path, release(tmp_path), PISA_REGISTRY="registry.example")
    assert done.returncode == 0, done.stderr
    assert f"image=registry.example/example-owner/pisa@{DIGEST}" in done.stdout


def test_a_changed_file_installs_nothing(tmp_path: Path) -> None:
    src = release(tmp_path)
    compose = src / "deploy" / "docker-compose.yml"
    compose.write_text(compose.read_text() + "\n# changed\n")
    done = install(tmp_path, src)
    assert done.returncode != 0
    assert "deploy/docker-compose.yml does not match the release's SHA256SUMS" in done.stderr
    assert not any((tmp_path / "home").iterdir())  # nothing moved in


def test_a_file_the_sums_do_not_list_installs_nothing(tmp_path: Path) -> None:
    src = release(tmp_path)
    sums = src / "SHA256SUMS"
    sums.write_text("".join(line for line in sums.read_text().splitlines(True) if "Caddyfile" not in line))
    done = install(tmp_path, src)
    assert done.returncode != 0 and "does not list deploy/caddy/Caddyfile" in done.stderr
    assert not any((tmp_path / "home").iterdir())


@pytest.mark.parametrize(
    "images", ["pisa ghcr.io/example-owner/pisa:1.2.3\n", "", "pisa ghcr.io/x/pisa@sha256:12\n"]
)
def test_an_image_without_a_digest_is_refused(tmp_path: Path, images: str) -> None:
    src = release(tmp_path)
    (src / "IMAGES").write_text(images)
    done = install(tmp_path, src)
    assert done.returncode != 0 and "no server image by digest" in done.stderr


def test_no_sums_no_install(tmp_path: Path) -> None:
    src = release(tmp_path)
    (src / "SHA256SUMS").unlink()
    done = install(tmp_path, src)
    assert done.returncode != 0 and "SHA256SUMS" in done.stderr
    assert not any((tmp_path / "home").iterdir())


def test_install_ps1_fetches_and_checks_the_same_bundle() -> None:
    text = (ROOT / "deploy" / "install.ps1").read_text()
    block = text[text.index("$Bundle = @(") : text.index("$ImageRe")]
    assert re.findall(r"'([^']+)'", block) == bundle()
    assert "SHA256SUMS" in text and "IMAGES" in text and "Get-FileHash" in text


def test_the_api_trusts_its_profiles_proxy_alone_whose_address_no_other_container_gets() -> None:
    """One trusted proxy address, held by the profile's own proxy (tailscale or caddy), outside
    the range Docker hands other containers' addresses from; the gateway stays .1 for a proxy on the host."""
    import ipaddress

    compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
    example = dict(
        line.split("=", 1) for line in (ROOT / "deploy" / ".env.example").read_text().splitlines()
        if re.match(r"^PISA_(SUBNET|GATEWAY|IP_RANGE|PROXY_IP)=", line)
    )  # fmt: skip
    trusted = re.findall(r"^ +PISA_TRUSTED_PROXIES: (.+)", compose, re.MULTILINE)
    assert trusted == ["${PISA_PROXY_IP:-172.29.0.10}/32"]
    assert re.findall(r"ipv4_address: (\S+)", compose) == ["${PISA_PROXY_IP:-172.29.0.10}"] * 2
    for name, default in (("SUBNET", "172.29.0.0/24"), ("IP_RANGE", "172.29.0.128/25"),
                          ("GATEWAY", "172.29.0.1"), ("PROXY_IP", "172.29.0.10")):  # fmt: skip
        assert f"${{PISA_{name}:-{default}}}" in compose and example[f"PISA_{name}"] == default
    subnet, dynamic = ipaddress.IPv4Network("172.29.0.0/24"), ipaddress.IPv4Network("172.29.0.128/25")
    proxy, gateway = ipaddress.IPv4Address("172.29.0.10"), ipaddress.IPv4Address("172.29.0.1")
    assert dynamic.subnet_of(subnet) and proxy in subnet and proxy not in dynamic and gateway not in dynamic


def test_install_ps1_makes_the_whole_install_folder_the_users_alone() -> None:
    """Read, not run (no PowerShell on the test machine): the owner-only rule goes on the whole
    PISA_HOME, inherited by everything in it, and a drive's root or a folder of other files is refused."""
    text = (ROOT / "deploy" / "install.ps1").read_text()
    tree = text[text.index("function Protect-Tree") : text.index("function Protect-File")]
    assert "/inheritance:r" in tree and "(OI)(CI)F" in tree and "S-1-5-18" in tree and "/reset /T" in tree
    main = text[text.index("Test-Platform\nTest-Docker") :]
    assert main.index("Test-Home $pisaHome") < main.index("Protect-Tree $pisaHome") < main.index("Get-Bundle")
    assert "if ($image) { Protect-Tree $pisaHome }" in main  # again, once the bundle and tls\ are in


# main() with the machine stubbed: `id`, `chown` and `docker` are shell functions that log their arguments.
MAIN_HARNESS = r"""
source "$SCRIPT"
check_platform() { :; }
check_docker() { :; }
fetch_bundle() { mkdir -p "$1/deploy"; RELEASE_IMAGE="$IMAGE"; }
id() { case "$1" in -u) printf '%s\n' "$FAKE_UID" ;; -g) printf '%s\n' "$FAKE_GID" ;; esac; }
chown() { printf 'chown %s\n' "$*" >> "$LOG"; }
docker() { printf 'docker %s\n' "$*" >> "$LOG"; }
main --mode local --yes --no-start
"""


@pytest.mark.parametrize(("uid", "user"), [("0", "10001:10001"), ("1000", "1000:1000")])
def test_as_root_the_folder_goes_to_the_images_user_and_init_runs_as_it(
    tmp_path: Path, uid: str, user: str
) -> None:
    """The root path (`pisa init --mode local` run by root failed): the deploy folder is given to uid 10001 on
    the host, then init runs as 10001, so it never chowns inside a container without capabilities. Any other
    user runs init as itself and chowns nothing."""
    script = tmp_path / "bin" / "install.sh"  # outside the checkout: the release path, not the in-place one
    script.parent.mkdir()
    shutil.copyfile(SCRIPT, script)
    log, home = tmp_path / "log", tmp_path / "home"
    environ = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "SCRIPT": str(script), "LOG": str(log), "IMAGE": IMAGE,
        "FAKE_UID": uid, "FAKE_GID": uid, "PISA_HOME": str(home), "PISA_VERSION": "v1.2.3",
        "PISA_REPO": "example-owner/pisa", "HOME": str(tmp_path),
    }  # fmt: skip
    done = subprocess.run(
        ["bash", "-c", MAIN_HARNESS], capture_output=True, text=True, env=environ, timeout=60
    )
    assert done.returncode == 0, done.stderr
    calls = log.read_text().splitlines()
    chowns = [c for c in calls if c.startswith("chown")]
    run = next(c for c in calls if c.startswith("docker compose"))
    assert f"--user {user} " in run and f"--image {IMAGE}" in run
    if uid == "0":
        assert chowns == [f"chown -R 10001:10001 {home}/deploy"]
        assert calls.index(chowns[0]) < calls.index(run)  # before init runs
    else:
        assert chowns == []


# --- a private repository: PISA_GITHUB_TOKEN ----------------------------------------------------------------
# A stand-in for curl on PATH: it serves the release from SRC as GitHub would for a private repository (raw
# files and the API answer only with the token; a release's download link never does), and logs each call
# without the token: its URL, its Accept header, whether the Authorization header was the expected one, and
# its arguments.
FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path

args, headers, out, url = sys.argv[1:], [], None, None
i = 0
while i < len(args):
    a = args[i]
    if a in ("--header", "-H"):
        headers.append(args[i + 1]); i += 2; continue
    if a in ("--output", "-o"):
        out = args[i + 1]; i += 2; continue
    if a in ("--proto", "--tlsv1.2") and a == "--proto":
        i += 2; continue
    if not a.startswith("-"):
        url = a
    i += 1
lines = []
for h in headers:
    lines += Path(h[1:]).read_text().splitlines() if h.startswith("@") else [h]
want = "Authorization: Bearer " + os.environ["EXPECT_TOKEN"]
other = any(l.lower().startswith("authorization") for l in lines)
auth = "auth" if want in lines else ("other-auth" if other else "-")
accept = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("accept:")), "-")
with open(os.environ["CURL_LOG"], "a") as log:
    log.write(f"{url} {accept} {auth}\n")
with open(os.environ["CURL_ARGV"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
src, private = Path(os.environ["SRC"]), os.environ.get("PRIVATE") == "1"
raw = "https://raw.invalid/example-owner/pisa/v1.2.3/"
dl = "https://release.invalid/example-owner/pisa/releases/download/v1.2.3/"
api = "https://api.invalid/repos/example-owner/pisa/releases/"
assets = {"101": "SHA256SUMS", "102": "IMAGES"}
body = None
if url.startswith(raw) and (auth == "auth" or not private):
    body = (src / url[len(raw):]).read_bytes() if (src / url[len(raw):]).is_file() else None
elif url.startswith(dl) and not private:
    body = (src / url[len(dl):]).read_bytes() if (src / url[len(dl):]).is_file() else None
elif url == api + "tags/v1.2.3" and auth == "auth":
    body = json.dumps({"name": "v1.2.3", "assets": [
        {"url": api + "assets/" + k, "id": int(k), "name": v, "uploader": {"login": "bot"}}
        for k, v in assets.items()
    ]}).encode()
elif url.startswith(api + "assets/") and auth == "auth" and accept == "application/octet-stream":
    name = assets.get(url[len(api + "assets/"):])
    body = (src / name).read_bytes() if name else None
if body is None:
    print("curl: (22) The requested URL returned error: 404", file=sys.stderr)
    sys.exit(22)
Path(out).write_bytes(body)
"""

TOKEN = "ghp_" + "Synthetic0Token0For0Tests0Only0000000"  # invented: no account has it

TOKEN_HARNESS = r"""
source "$SCRIPT"
check_platform() { :; }
check_docker() { :; }
docker() {
  if [ -n "${PISA_GITHUB_TOKEN:-}" ]; then echo "docker sees the token" >> "$LOG"; fi
  printf 'docker %s\n' "$*" >> "$LOG"
  printf 'PISA_IMAGE=%s\n' "$PISA_IMAGE" >> "$PISA_HOME/deploy/.env"  # as init would write it
}
main --mode local --yes --no-start
"""


def private_install(
    tmp_path: Path, token: str | None, private: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    src = release(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    script = tmp_path / "dl" / "install.sh"  # outside the checkout: the release path
    script.parent.mkdir()
    shutil.copyfile(SCRIPT, script)
    home = tmp_path / "home"
    environ = {
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin", "SCRIPT": str(script), "SRC": str(src),
        "LOG": str(tmp_path / "log"), "CURL_LOG": str(tmp_path / "curl.log"),
        "CURL_ARGV": str(tmp_path / "argv"), "PRIVATE": "1" if private else "0", "EXPECT_TOKEN": TOKEN,
        "HOME": str(tmp_path), "PISA_HOME": str(home),
        "PISA_REPO": "example-owner/pisa", "PISA_VERSION": "v1.2.3", "PISA_RAW_BASE": "https://raw.invalid",
        "PISA_RELEASE_BASE": "https://release.invalid", "PISA_API_BASE": "https://api.invalid",
    }  # fmt: skip
    if token is not None:
        environ["PISA_GITHUB_TOKEN"] = token
    done = subprocess.run(
        ["bash", "-c", TOKEN_HARNESS], capture_output=True, text=True, env=environ, timeout=60
    )
    return done, home


def test_without_a_token_nothing_changes(tmp_path: Path) -> None:
    done, home = private_install(tmp_path, None, private=False)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "curl.log").read_text().splitlines()
    assert all(c.endswith(" - -") for c in calls), calls  # no Authorization, no Accept header
    assert not any("api.invalid" in c for c in calls)  # the API path is for a token only
    assets = [c.split()[0].rsplit("/", 1)[1] for c in calls if "release.invalid" in c]
    assert assets == ["SHA256SUMS", "IMAGES"]
    assert f"PISA_IMAGE={IMAGE}" in (home / "deploy" / ".env").read_text()


def test_a_private_release_installs_with_the_token_which_is_never_shown_or_kept(tmp_path: Path) -> None:
    done, home = private_install(tmp_path, TOKEN, private=True)
    assert done.returncode == 0, done.stderr
    assert "Using PISA_GITHUB_TOKEN" in done.stdout
    calls = (tmp_path / "curl.log").read_text().splitlines()
    raw = [c for c in calls if "raw.invalid" in c]
    assert raw and all(c.endswith(" - auth") for c in raw)  # the bundle with the token
    assert not any("release.invalid" in c for c in calls)  # a private release's download links take no token
    assert [c for c in calls if "api.invalid" in c] == [
        "https://api.invalid/repos/example-owner/pisa/releases/tags/v1.2.3 application/vnd.github+json auth",
        "https://api.invalid/repos/example-owner/pisa/releases/assets/101 application/octet-stream auth",
        "https://api.invalid/repos/example-owner/pisa/releases/assets/102 application/octet-stream auth",
    ]
    assert f"PISA_IMAGE={IMAGE}" in (home / "deploy" / ".env").read_text()
    # never on a command line, in the output, in .env, in the install folder, or seen by docker
    assert TOKEN not in (tmp_path / "argv").read_text()
    assert TOKEN not in done.stdout + done.stderr
    assert "docker sees the token" not in (tmp_path / "log").read_text()
    for path in home.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(errors="replace"), path


def test_a_private_release_without_the_token_installs_nothing(tmp_path: Path) -> None:
    done, home = private_install(tmp_path, None, private=True)
    assert done.returncode != 0 and "could not fetch" in done.stderr
    assert not (home / "deploy" / ".env").exists()


def test_a_wrong_token_is_refused_without_being_printed(tmp_path: Path) -> None:
    wrong = "ghp_" + "Another0Invented0Token0000000000000000"
    done, home = private_install(tmp_path, wrong, private=True)
    assert done.returncode != 0 and "could not fetch" in done.stderr
    assert wrong not in done.stdout + done.stderr and wrong not in (tmp_path / "argv").read_text()
    assert not (home / "deploy" / ".env").exists()


def test_install_ps1_sends_the_token_as_a_header_and_takes_assets_through_the_api() -> None:
    """Read, not run (no PowerShell on the test machine): the same two paths as install.sh."""
    text = (ROOT / "deploy" / "install.ps1").read_text()
    assert "$AuthHeaders['Authorization'] = \"Bearer $($env:PISA_GITHUB_TOKEN)\"" in text
    assert text.count("-Headers $AuthHeaders") >= 2  # the bundle and Get-Fetched
    asset = text[text.index("function Get-ReleaseAsset") : text.index("function Test-BundleSums")]
    assert "if (-not $AuthHeaders.Count)" in asset and "/releases/download/" in asset
    assert '$api = "$ApiBase/repos/$($env:PISA_REPO)/releases"' in asset
    assert '"$api/tags/$version"' in asset and '"$api/assets/' in asset
    assert "application/octet-stream" in asset
    assert "Remove-Item Env:PISA_GITHUB_TOKEN" in text
    # the token itself is only ever put in the header
    assert [line.strip() for line in text.splitlines() if "PISA_GITHUB_TOKEN)" in line] == [
        "if ($env:PISA_GITHUB_TOKEN) { $AuthHeaders['Authorization'] = \"Bearer $($env:PISA_GITHUB_TOKEN)\" }"
    ]


def _subnet_shell(tmp_path, used: str, env_text: str = "", pisa_net: str = "") -> tuple[str, str]:
    """Run install.sh's choose_subnet against a fake `docker` whose networks use `used`."""
    script = SCRIPT.read_text()
    start = script.index("ip_int() {")
    end = script.index("AUTH_HEADER=")
    env = tmp_path / ".env"
    env.write_text(env_text)
    prog = (
        'say() { echo "say: $*"; }\ndie() { echo "die: $*"; exit 1; }\n'
        'docker() { case "$1 $2 $3" in "network inspect pisa_default") echo "${PISA_NET:-}";; esac;\n'
        '  case "$1 $2" in "network ls") echo n1;;\n'
        '    "network inspect") [ "$3" = pisa_default ] || echo "$USED";; esac; }\n'
        + script[start:end]
        + f'\nchoose_subnet "{env}"\n'
    )
    env_vars = {"USED": used, "PISA_NET": pisa_net, "PATH": "/usr/bin:/bin"}
    run = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env_vars, check=False)
    return run.stdout + run.stderr, env.read_text()


def test_the_network_takes_the_first_free_range_and_writes_all_four(tmp_path):
    _out, env = _subnet_shell(tmp_path, "172.29.0.0/16 10.213.0.0/23 fd00::/64")
    assert "PISA_SUBNET=10.213.2.0/24" in env
    assert "PISA_IP_RANGE=10.213.2.128/25" in env
    assert "PISA_GATEWAY=10.213.2.1" in env
    assert "PISA_PROXY_IP=10.213.2.10" in env


def test_a_chosen_network_is_kept_on_a_rerun(tmp_path):
    _out, env = _subnet_shell(tmp_path, "", "PISA_SUBNET=10.213.7.0/24\n")
    assert env == "PISA_SUBNET=10.213.7.0/24\n"


def test_no_free_range_stops_with_what_to_do(tmp_path):
    out, env = _subnet_shell(tmp_path, "10.0.0.0/8 172.16.0.0/12")
    assert "die: No free address range" in out
    assert "PISA_SUBNET" not in env


# --- the host's time zone: PISA_HOST_TZ, the default of init's time zone question ---------------------------
# Stand-ins on PATH for the commands install.sh asks: each answers from an environment variable, or fails as
# the real one does when it can't tell (FAKE_* unset).
FAKE_TOOLS = {
    "uname": 'case "$1" in -s) echo "$FAKE_OS" ;; -n) echo testhost ;; *) echo x86_64 ;; esac',
    "readlink": '[ -n "${FAKE_LINK:-}" ] || exit 1; echo "$FAKE_LINK"',
    "timedatectl": '[ -n "${FAKE_TDC:-}" ] || exit 1; echo "$FAKE_TDC"',
    "systemsetup": 'echo "You need administrator access to run this tool... exiting!"; exit 1',
}


def _tz_env(tmp_path: Path, os_name: str, **fake: str) -> dict[str, str]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in FAKE_TOOLS.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin", "SCRIPT": str(SCRIPT), "FAKE_OS": os_name,
            "ZONE_FILE_AT": str(tmp_path / "timezone"), **fake}  # fmt: skip


@pytest.mark.parametrize(
    ("os_name", "fake", "zone_file", "zone"),
    [
        ("Darwin", {"FAKE_LINK": "/var/db/timezone/zoneinfo/Europe/Rome"}, None, "Europe/Rome"),
        ("Darwin", {"FAKE_LINK": "/usr/share/zoneinfo/America/New_York"}, None, "America/New_York"),
        ("Darwin", {}, None, ""),  # no link, and systemsetup wants an administrator: nothing
        ("Linux", {"FAKE_TDC": "Europe/Rome", "FAKE_LINK": "/usr/share/zoneinfo/UTC"}, None, "Europe/Rome"),
        ("Linux", {"FAKE_LINK": "/usr/share/zoneinfo/UTC"}, "Europe/Berlin\n", "Europe/Berlin"),
        ("Linux", {"FAKE_LINK": "../usr/share/zoneinfo/posix/Asia/Tokyo"}, None, "Asia/Tokyo"),
        ("Linux", {"FAKE_TDC": "not a zone; rm -rf /"}, None, ""),  # refused, never passed on
        ("Linux", {}, None, ""),
    ],
)  # fmt: skip
def test_host_tz_reads_this_machines_zone(
    tmp_path: Path, os_name: str, fake: dict[str, str], zone_file: str | None, zone: str
) -> None:
    if zone_file is not None:
        (tmp_path / "timezone").write_text(zone_file)
    environ = _tz_env(tmp_path, os_name, **fake)
    done = subprocess.run(
        ["bash", "-c", 'source "$SCRIPT"; ZONE_FILE="$ZONE_FILE_AT"; printf "zone=[%s]" "$(host_tz)"'],
        capture_output=True, text=True, env=environ, timeout=30,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"zone=[{zone}]"


@pytest.mark.parametrize(("fake", "passed"), [({"FAKE_TDC": "Europe/Rome"}, True), ({}, False)])
def test_install_sh_passes_the_host_zone_to_init(tmp_path: Path, fake: dict[str, str], passed: bool) -> None:
    script = tmp_path / "bin" / "install.sh"  # the release path, not the in-place one
    script.parent.mkdir()
    shutil.copyfile(SCRIPT, script)
    log, home = tmp_path / "log", tmp_path / "home"
    environ = {
        **_tz_env(tmp_path, "Linux", **fake), "SCRIPT": str(script), "LOG": str(log), "IMAGE": IMAGE,
        "FAKE_UID": "1000", "FAKE_GID": "1000", "PISA_HOME": str(home), "PISA_VERSION": "v1.2.3",
        "PISA_REPO": "example-owner/pisa", "HOME": str(tmp_path),
    }  # fmt: skip
    harness = MAIN_HARNESS.replace('source "$SCRIPT"\n', 'source "$SCRIPT"\nZONE_FILE="$ZONE_FILE_AT"\n')
    done = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=environ, timeout=60)
    assert done.returncode == 0, done.stderr
    run = next(c for c in log.read_text().splitlines() if c.startswith("docker compose"))
    assert ("--env PISA_HOST_TZ=Europe/Rome " in run) is passed
    assert passed or "PISA_HOST_TZ" not in run
    assert run.index("--env PISA_HOST_NAME=testhost") < run.index(" api pisa init ")


def test_install_ps1_passes_the_host_zone_converted_to_iana() -> None:
    """Read, not run: the Windows zone converted with .NET 6+, passed only when that worked."""
    text = (ROOT / "deploy" / "install.ps1").read_text()
    assert "[System.TimeZoneInfo]::TryConvertWindowsIdToIanaId((Get-TimeZone).Id, [ref]$iana)" in text
    assert "$zoneArgs = @('--env', \"PISA_HOST_TZ=$iana\")" in text
    assert "@zoneArgs api pisa init @initArgs @initFlags" in text
    block = text[text.index("$zoneArgs = @()") : text.index("@zoneArgs api pisa init")]
    assert "catch { $zoneArgs = @() }" in block  # Windows PowerShell 5.1: nothing passed, no error


# --- the installer starts Pisa and waits until it is healthy ------------------------------------------------
# main() with docker stubbed: `compose run` (init) and `compose up -d` are logged; `compose ps` answers with
# the next of STATE1, STATE2, ... (the last one again once they run out); sleep is logged, not slept.
START_HARNESS = r"""
source "$SCRIPT"
check_platform() { :; }
check_docker() { :; }
fetch_bundle() { mkdir -p "$1/deploy"; RELEASE_IMAGE="$IMAGE"; }
host_tz() { :; }
START_TIMEOUT=15
sleep() { printf 'sleep %s\n' "$*" >> "$LOG"; }
docker() {
  case " $* " in
    *" ps "*)
      local n; n=$(( $(cat "$LOG.ps" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$LOG.ps"
      local var="STATE$n"; [ -n "${!var:-}" ] || var="STATE$(( n > LAST ? LAST : n ))"
      printf '%b\n' "${!var}" ;;
    *" network inspect pisa_default "*) printf '%s\n' "${NET:-}" ;;
    *" up -d "*) printf 'docker %s\n' "$*" >> "$LOG"; return "${UP_CODE:-0}" ;;
    *) printf 'docker %s\n' "$*" >> "$LOG" ;;
  esac
}
main --mode local --yes "$@"
"""


def start(
    tmp_path: Path, *argv: str, env_text: str = "", **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    script = tmp_path / "bin" / "install.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCRIPT, script)
    log, home = tmp_path / "log", tmp_path / "home"
    if env_text:
        (home / "deploy").mkdir(parents=True)
        (home / "deploy" / ".env").write_text(env_text)
    pss = [k for k in env if re.fullmatch(r"STATE\d+", k)]
    environ = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "SCRIPT": str(script), "LOG": str(log), "IMAGE": IMAGE,
        "PISA_HOME": str(home), "PISA_VERSION": "v1.2.3", "PISA_REPO": "example-owner/pisa",
        "HOME": str(tmp_path), "LAST": str(len(pss)), **env,
    }  # fmt: skip
    done = subprocess.run(
        ["bash", "-c", START_HARNESS, "harness", *argv],
        capture_output=True,
        text=True,
        env=environ,
        timeout=60,
    )
    return done, (log.read_text().splitlines() if log.exists() else [])


HEALTHY = r"postgres|running|healthy|0\napi|running|healthy|0\nworker|running||0\nmigrate|exited||0"


def test_the_installer_starts_pisa_and_waits_until_it_is_healthy(tmp_path: Path) -> None:
    done, calls = start(tmp_path, STATE1=r"postgres|running|starting|0\napi|created||0", STATE2=HEALTHY)
    assert done.returncode == 0, done.stderr
    run = next(c for c in calls if " run " in c)
    up = next(c for c in calls if c.endswith(" up -d"))
    assert calls.index(run) < calls.index(up)  # init first, then up
    assert "--started" in run and "--no-start" not in run
    assert f"-f {tmp_path}/home/deploy/docker-compose.yml --env-file {tmp_path}/home/deploy/.env up -d" in up
    assert calls.count("sleep 5") == 1  # one wait: the second look found it healthy
    out = done.stdout
    assert "waiting for postgres(starting) api(created)" in out
    assert out.rstrip().endswith("exec worker pisa pair     # the QR code your phone scans")
    tail = out[out.index("Pisa is running.") :]
    assert "exec worker pisa setup" in tail and " up -d" not in tail  # one list, without `up`


def test_a_service_that_never_gets_healthy_is_named_with_its_logs_command(tmp_path: Path) -> None:
    done, calls = start(
        tmp_path, STATE1=r"postgres|running|healthy|0\napi|running|unhealthy|0\nmigrate|exited||1"
    )
    assert done.returncode == 1
    assert "Pisa is running" not in done.stdout
    err = done.stderr
    assert "did not become healthy within 15 seconds. Not ready: api(unhealthy) migrate(exited 1)" in err
    compose = (
        f"docker compose -f {tmp_path}/home/deploy/docker-compose.yml --env-file {tmp_path}/home/deploy/.env"
    )
    assert f"{compose} logs --tail 50 api" in err and f"{compose} logs --tail 50 migrate" in err
    assert "postgres" not in err.split("Not ready:")[1]
    assert calls.count("sleep 5") == 3  # 0, 5, 10, then 15: given up


def test_a_failed_up_says_what_to_do_about_docker_desktop(tmp_path: Path) -> None:
    done, _ = start(tmp_path, UP_CODE="1", STATE1=HEALTHY)
    assert done.returncode == 1 and "input/output" in done.stderr and "Quit Docker Desktop" in done.stderr


def test_no_start_only_writes_the_env(tmp_path: Path) -> None:
    done, calls = start(tmp_path, "--no-start", STATE1=HEALTHY)
    assert done.returncode == 0, done.stderr
    run = next(c for c in calls if " run " in c)
    assert "--started" not in run and "--no-start" not in run
    assert not any(c.endswith(" up -d") for c in calls) and "Pisa is running" not in done.stdout


def test_a_rerun_on_a_set_up_install_starts_it(tmp_path: Path) -> None:
    configured = "POSTGRES_PASSWORD=" + "a1" * 24 + "\nPISA_SUBNET=10.213.0.0/24\n"
    done, calls = start(tmp_path, env_text=configured, STATE1=HEALTHY)
    assert done.returncode == 0, done.stderr
    assert not any(" run " in c for c in calls) and any(c.endswith(" up -d") for c in calls)
    assert "already set up" in done.stdout and "Pisa is running." in done.stdout
    done, calls = start(tmp_path / "again", "--no-start", env_text=configured, STATE1=HEALTHY)
    assert done.returncode == 0 and not any(c.endswith(" up -d") for c in calls)
    assert "Start it with: docker compose" in done.stdout


def test_install_ps1_starts_pisa_the_same_way() -> None:
    """Read, not run (no PowerShell here): init runs with --started, then Start-Pisa (`up -d`, a look every
    5 s for up to 180 s, the logs command of what isn't healthy, exit 1) and the same two next steps;
    --no-start is the script's own and never reaches init; a set-up install is started too."""
    text = (ROOT / "deploy" / "install.ps1").read_text()
    assert "$StartTimeout = 180" in text and "$StartPoll = 5" in text
    start = text[text.index("function Start-Pisa") : text.index("function Show-RunningSteps")]
    assert "up -d" in start and "'{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}'" in start
    assert "logs --tail 50 $svc" in start and "exit 1" in start and "input/output" in start
    steps = text[text.index("function Show-RunningSteps") :]
    assert "exec worker pisa setup" in steps and "exec worker pisa pair" in steps
    assert "$initFlags = @($args | Where-Object { $_ -ne '--no-start' })" in text
    assert "if ($start) { $initArgs += @('--started') }" in text
    rerun = text[
        text.index("# Run again on an install that is already set up") : text.index("$initArgs = @(")
    ]
    assert "Start-Pisa $deploy" in rerun and "Show-RunningSteps $deploy" in rerun
    tail = text[text.index("@zoneArgs api pisa init") :]
    assert "if ($code -eq 0 -and $start) {" in tail and "Start-Pisa $deploy" in tail


def test_tailscale_without_a_key_does_not_wait_for_it_and_says_how_to_add_it(tmp_path: Path) -> None:
    configured = (
        "POSTGRES_PASSWORD=" + "a1" * 24 + "\nPISA_SUBNET=10.213.0.0/24\n"
        "COMPOSE_PROFILES=tailscale\nTS_AUTHKEY=\n"
    )
    state = HEALTHY + r"\ntailscale|restarting||1\nvoice-relay|created||0"
    done, calls = start(tmp_path, env_text=configured, STATE1=state)
    assert done.returncode == 0, done.stderr
    assert calls.count("sleep 5") == 0  # the key-less tailnet side is not waited on
    out = done.stdout
    assert "Tailscale has no auth key yet" in out and "TS_AUTHKEY=%s" in out and "Pisa is running." in out


def test_tailscale_with_a_key_is_waited_on(tmp_path: Path) -> None:
    configured = (
        "POSTGRES_PASSWORD=" + "a1" * 24 + "\nPISA_SUBNET=10.213.0.0/24\nCOMPOSE_PROFILES=tailscale\n"
        f"TS_AUTHKEY={tailscale_key('auth', 'example')}\n"
    )
    state = HEALTHY + r"\ntailscale|restarting||1"
    done, _ = start(tmp_path, env_text=configured, STATE1=state)
    assert done.returncode == 1 and "tailscale(restarting)" in done.stderr


# --- a re-run with a new PISA_VERSION is the update ---------------------------------------------------------
# The real fetch_bundle with curl stubbed (FAKE_CURL serves the release v1.2.3, whose IMAGES names IMAGE), and
# docker stubbed (every service healthy at once).
UPDATE_HARNESS = r"""
source "$SCRIPT"
check_platform() { :; }
check_docker() { :; }
host_tz() { :; }
sleep() { :; }
docker() {
  printf 'docker %s\n' "$*" >> "$LOG"
  case " $* " in
    *" ps "*) printf '%b\n' "$HEALTHY" ;;
  esac
}
main "$@"
"""
OLD_IMAGE = "ghcr.io/example-owner/pisa@sha256:" + "cd" * 32
SET_UP = (
    "# Pisa's settings (synthetic)\nPOSTGRES_PASSWORD=" + "a1" * 24 + "\n"
    f"PISA_IMAGE={OLD_IMAGE}\n"
    "PISA_LANGUAGE=it\n\nPISA_SUBNET=10.213.0.0/24\nPISA_IP_RANGE=10.213.0.128/25\nPISA_GATEWAY=10.213.0.1\n"
    "PISA_PROXY_IP=10.213.0.10\n"
)


def update(tmp_path: Path, env_text: str) -> tuple[subprocess.CompletedProcess[str], str, list[str]]:
    """install.sh run again on a set-up install (`env_text` its .env): the result, the .env after, docker's
    calls."""
    src = release(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    script = tmp_path / "dl" / "install.sh"  # outside the checkout: the release path
    script.parent.mkdir()
    shutil.copyfile(SCRIPT, script)
    home = tmp_path / "home"
    (home / "deploy").mkdir(parents=True)
    env = home / "deploy" / ".env"
    env.write_text(env_text)
    environ = {
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin", "SCRIPT": str(script), "SRC": str(src),
        "LOG": str(tmp_path / "log"), "CURL_LOG": str(tmp_path / "curl.log"),
        "CURL_ARGV": str(tmp_path / "argv"), "PRIVATE": "0", "EXPECT_TOKEN": TOKEN, "HEALTHY": HEALTHY,
        "HOME": str(tmp_path), "PISA_HOME": str(home),
        "PISA_REPO": "example-owner/pisa", "PISA_VERSION": "v1.2.3", "PISA_RAW_BASE": "https://raw.invalid",
        "PISA_RELEASE_BASE": "https://release.invalid",
    }  # fmt: skip
    done = subprocess.run(
        ["bash", "-c", UPDATE_HARNESS], capture_output=True, text=True, env=environ, timeout=60
    )
    log = tmp_path / "log"
    return done, env.read_text(), (log.read_text().splitlines() if log.exists() else [])


def test_a_rerun_with_a_new_release_updates_the_image_alone_and_starts_pisa(tmp_path: Path) -> None:
    done, env, calls = update(tmp_path, SET_UP)
    assert done.returncode == 0, done.stderr
    assert env == SET_UP.replace(f"PISA_IMAGE={OLD_IMAGE}", f"PISA_IMAGE={IMAGE}")  # every other line kept
    assert "Updated to v1.2.3" in done.stdout and "Pisa is running." in done.stdout
    assert not any(" run " in c for c in calls)  # no init: no --force needed
    assert any(c.endswith(" up -d") for c in calls)


def test_an_env_without_the_image_line_gets_it_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    without = SET_UP.replace(f"PISA_IMAGE={OLD_IMAGE}\n", "")
    done, env, _ = update(tmp_path, without.rstrip("\n"))  # its last line without a newline
    assert done.returncode == 0, done.stderr
    assert env == without + f"PISA_IMAGE={IMAGE}\n"
    done, again, _ = update(tmp_path / "again", env)
    assert done.returncode == 0, done.stderr
    assert again == env and "already v1.2.3's" in done.stdout


def test_install_ps1_updates_the_image_the_same_way() -> None:
    """Read, not run (no PowerShell here): a set-up install's re-run sets the release's image alone, then
    starts Pisa."""
    text = (ROOT / "deploy" / "install.ps1").read_text()
    fn = text[text.index("function Set-Image") : text.index("Test-Platform\nTest-Docker")]
    assert "'^PISA_IMAGE='" in fn and '"PISA_IMAGE=$Ref"' in fn and "WriteAllText" in fn and '"`n"' in fn
    rerun = text[
        text.index("# Run again on an install that is already set up") : text.index("$initArgs = @(")
    ]
    assert "if ($releaseImage) { Set-Image $envFile $releaseImage }" in rerun
    assert rerun.index("Set-Image") < rerun.index("Start-Pisa $deploy")


def test_an_upgrade_keeps_the_network_the_install_already_runs_on(tmp_path):
    """An install from before the automatic choice (the compose default) keeps its network, all four lines."""
    _out, env = _subnet_shell(
        tmp_path, "172.29.0.0/24 172.17.0.0/16", pisa_net="172.29.0.0/24 172.29.0.128/25 172.29.0.1"
    )
    assert "PISA_SUBNET=172.29.0.0/24" in env
    assert "PISA_IP_RANGE=172.29.0.128/25" in env
    assert "PISA_GATEWAY=172.29.0.1" in env
    assert "PISA_PROXY_IP=172.29.0.10" in env


def test_a_changed_network_takes_the_stack_down_first_and_keeps_the_volumes(tmp_path: Path) -> None:
    configured = "POSTGRES_PASSWORD=" + "a1" * 24 + "\nPISA_SUBNET=10.213.0.0/24\n"
    done, calls = start(
        tmp_path, env_text=configured, STATE1=HEALTHY, NET="172.29.0.0/24 172.29.0.128/25 172.29.0.1"
    )
    assert done.returncode == 0, done.stderr
    down = [c for c in calls if c.endswith(" down")]
    up = next(c for c in calls if c.endswith(" up -d"))
    assert down and calls.index(down[0]) < calls.index(up)
    assert not any(" -v" in c for c in down)
    assert "network changes from 172.29.0.0/24 to 10.213.0.0/24" in done.stdout


def test_the_same_network_is_not_taken_down(tmp_path: Path) -> None:
    configured = "POSTGRES_PASSWORD=" + "a1" * 24 + "\nPISA_SUBNET=10.213.0.0/24\n"
    done, calls = start(
        tmp_path, env_text=configured, STATE1=HEALTHY, NET="10.213.0.0/24 10.213.0.128/25 10.213.0.1"
    )
    assert done.returncode == 0, done.stderr
    assert not any(c.endswith(" down") for c in calls)


PIPED_STUBS = r"""
check_platform() { :; }
check_docker() { :; }
id() { case "$1" in -u) printf '%s\n' 1000 ;; -g) printf '%s\n' 1000 ;; esac; }
docker() { printf 'docker %s\n' "$*" >> "$LOG"; }
"""


def _piped(text: str, tail: str) -> str:
    """The script as `curl … | bash` gives it: on stdin, no file (BASH_SOURCE is empty), with test stubs
    placed after its functions and before its own run."""
    body = text[: text.rindex("# Run, unless sourced")]
    return body + PIPED_STUBS + tail


def test_piped_into_bash_it_installs_without_a_script_file(tmp_path: Path) -> None:
    """`curl … install.sh | PISA_REPO=… PISA_VERSION=v0.1.1 bash` died on "BASH_SOURCE[0]: unbound variable":
    piped, there is no script file, so it is never a checkout and init doesn't read the pipe."""
    log, home = tmp_path / "log", tmp_path / "home"
    stdin = _piped(
        SCRIPT.read_text(),
        'fetch_bundle() { mkdir -p "$1/deploy"; RELEASE_IMAGE="$IMAGE"; }\n'
        "main --mode local --yes --no-start\n",
    )
    environ = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LOG": str(log), "IMAGE": IMAGE, "PISA_HOME": str(home),
        "PISA_VERSION": "v1.2.3", "PISA_REPO": "example-owner/pisa", "HOME": str(tmp_path),
    }  # fmt: skip
    done = subprocess.run(["bash"], input=stdin, capture_output=True, text=True, env=environ, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "unbound variable" not in done.stderr
    assert "Using this checkout" not in done.stdout
    assert any(c.startswith("docker compose") and " pisa init " in c for c in log.read_text().splitlines())


def test_piped_without_pisa_repo_says_how_to_run_it(tmp_path: Path) -> None:
    """Piped with only PISA_VERSION (the owner's one-liner), it stops with the line to run, not an unbound
    variable."""
    stdin = _piped(SCRIPT.read_text(), "main --mode local --yes --no-start\n")
    environ = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LOG": str(tmp_path / "log"),
        "PISA_HOME": str(tmp_path / "h"), "PISA_VERSION": "v0.1.1", "HOME": str(tmp_path),
    }  # fmt: skip
    done = subprocess.run(["bash"], input=stdin, capture_output=True, text=True, env=environ, timeout=60)
    assert done.returncode != 0
    assert "unbound variable" not in done.stderr
    assert "PISA_REPO=<owner>/<repo> PISA_VERSION=v0.1.1 bash install.sh" in done.stderr
