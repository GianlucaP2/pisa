# SPDX-License-Identifier: AGPL-3.0-only
"""deploy/install.sh checks a release's bundle against its SHA256SUMS and takes the image by digest from its
IMAGES (E1-PACK PK-2), with the downloads replaced by local copies: a changed or unlisted file installs
nothing, and an image without a digest is refused. install.ps1 fetches the same files. Synthetic names
only."""

# ruff: noqa: S603, S607 (bash runs the repository's own install script)

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
    """E1-PACK Info: one trusted proxy address, held by the profile's own proxy (tailscale or caddy), outside
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
    """E1-PACK PK-3 (read, not run: no PowerShell on the test machine): the owner-only rule goes on the whole
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
main --mode local --yes
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
