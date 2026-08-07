"""Download and install verified updates published as GitHub releases."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPOSITORY = "NaveDanan/Mouse-Without-Borders-for-Linux"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PACKAGE_NAME = "powertoys-mouse-without-borders"
REQUEST_TIMEOUT = 10.0
MAX_RELEASE_RESPONSE = 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 128 * 1024
DEBIAN_ARCHIVE_MAGIC = b"!<arch>\n"


class UpdateError(RuntimeError):
    """An update could not be checked, downloaded, or installed safely."""


@dataclass(frozen=True, slots=True)
class UpdateRelease:
    """A newer stable GitHub release and its architecture-specific package."""

    version: str
    tag: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str


def version_key(version: str) -> tuple[int, int, int]:
    """Return a comparable key for this project's stable semantic versions."""

    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    if match is None:
        raise UpdateError(f"unsupported release version: {version}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def system_architecture() -> str:
    """Return the Debian architecture used in release asset names."""

    try:
        result = subprocess.run(
            ["/usr/bin/dpkg", "--print-architecture"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        architecture = result.stdout.strip()
        if architecture:
            return architecture
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }.get(platform.machine().lower(), platform.machine().lower())


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{PACKAGE_NAME}-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _open(
    opener: Callable[..., object] | None,
    request: urllib.request.Request,
    *,
    timeout: float,
):
    return (opener or urllib.request.urlopen)(request, timeout=timeout)


def _release_from_payload(
    payload: object,
    current_version: str,
    architecture: str,
) -> UpdateRelease | None:
    if not isinstance(payload, dict):
        raise UpdateError("GitHub returned invalid release metadata")
    tag = str(payload.get("tag_name", ""))
    latest_version = tag.removeprefix("v")
    if version_key(latest_version) <= version_key(current_version):
        return None

    expected_name = f"{PACKAGE_NAME}_{latest_version}_{architecture}.deb"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("the release does not contain package assets")
    asset = next(
        (
            candidate
            for candidate in assets
            if isinstance(candidate, dict) and candidate.get("name") == expected_name
        ),
        None,
    )
    if asset is None:
        raise UpdateError(f"the release has no {architecture} package")

    asset_url = str(asset.get("browser_download_url", ""))
    trusted_prefix = f"https://github.com/{REPOSITORY}/releases/download/"
    if not asset_url.startswith(trusted_prefix):
        raise UpdateError("the release package has an untrusted download URL")
    digest = str(asset.get("digest", ""))
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise UpdateError("the release package does not have a SHA-256 digest")
    try:
        size = int(asset.get("size", 0))
    except (TypeError, ValueError) as exc:
        raise UpdateError("the release package has an invalid size") from exc
    if size <= len(DEBIAN_ARCHIVE_MAGIC):
        raise UpdateError("the release package has an invalid size")

    page_url = str(payload.get("html_url", ""))
    if not page_url.startswith(f"https://github.com/{REPOSITORY}/releases/"):
        page_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    return UpdateRelease(
        version=latest_version,
        tag=tag,
        page_url=page_url,
        asset_name=expected_name,
        asset_url=asset_url,
        asset_size=size,
        sha256=digest.split(":", 1)[1].lower(),
    )


def check_for_update(
    current_version: str,
    *,
    architecture: str | None = None,
    opener: Callable[..., object] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> UpdateRelease | None:
    """Return the latest compatible stable release when it is newer."""

    try:
        response = _open(opener, _request(LATEST_RELEASE_URL), timeout=timeout)
        with response:  # type: ignore[attr-defined]
            data = response.read(MAX_RELEASE_RESPONSE + 1)  # type: ignore[attr-defined]
    except (OSError, TimeoutError) as exc:
        raise UpdateError("could not contact GitHub") from exc
    if len(data) > MAX_RELEASE_RESPONSE:
        raise UpdateError("GitHub returned oversized release metadata")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub returned invalid release metadata") from exc
    return _release_from_payload(
        payload,
        current_version,
        architecture or system_architecture(),
    )


def default_download_directory() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "powertoys-mwb-linux" / "updates"


def download_release(
    release: UpdateRelease,
    *,
    directory: Path | None = None,
    opener: Callable[..., object] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Path:
    """Download a release package atomically and verify GitHub's digest."""

    destination_directory = directory or default_download_directory()
    destination_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination_directory, 0o700)
    destination = destination_directory / release.asset_name
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{release.asset_name}.", suffix=".part", dir=destination_directory
    )
    digest = hashlib.sha256()
    downloaded = 0
    try:
        try:
            response = _open(opener, _request(release.asset_url), timeout=timeout)
            with response, os.fdopen(temporary_fd, "wb") as output:  # type: ignore[attr-defined]
                temporary_fd = -1
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > release.asset_size:
                        raise UpdateError("the downloaded package is larger than expected")
                    digest.update(chunk)
                    output.write(chunk)
        except (OSError, TimeoutError) as exc:
            raise UpdateError("could not download the update") from exc
        if downloaded != release.asset_size:
            raise UpdateError("the downloaded package size does not match the release")
        if digest.hexdigest() != release.sha256:
            raise UpdateError("the downloaded package failed SHA-256 verification")
        temporary = Path(temporary_name)
        with temporary.open("rb") as package:
            if package.read(len(DEBIAN_ARCHIVE_MAGIC)) != DEBIAN_ARCHIVE_MAGIC:
                raise UpdateError("the downloaded file is not a Debian package")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass


def install_package(
    package: Path,
    expected_version: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Install a downloaded package, asking PolicyKit for privilege if needed."""

    package = package.resolve(strict=True)
    apt_get = shutil.which("apt-get")
    if not apt_get:
        raise UpdateError("apt-get is unavailable")
    command = [apt_get, "install", "--yes", str(package)]
    if os.geteuid() != 0:
        pkexec = shutil.which("pkexec")
        if not pkexec:
            raise UpdateError("pkexec is unavailable")
        command.insert(0, pkexec)
    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("the package installer could not run") from exc
    if result.returncode != 0:
        raise UpdateError("the update was not installed")

    try:
        installed = runner(
            ["/usr/bin/dpkg-query", "--show", "--showformat=${Version}", PACKAGE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("the installed update could not be verified") from exc
    if installed != expected_version:
        raise UpdateError(
            f"the installed package is {installed or 'unknown'}, expected {expected_version}"
        )


def schedule_relaunch(command: list[str] | None = None) -> subprocess.Popen[bytes]:
    """Start a detached helper that relaunches after this process has exited."""

    executable = shutil.which(PACKAGE_NAME)
    launch_command = command or (
        [executable] if executable else [sys.executable, "-m", "mwb_linux"]
    )
    waiter = """\
import os
import select
import sys

parent = int(sys.argv[1])
pidfd = os.pidfd_open(parent)
poller = select.poll()
poller.register(pidfd, select.POLLIN)
poller.poll()
os.execvp(sys.argv[2], sys.argv[2:])
"""
    try:
        return subprocess.Popen(
            [sys.executable, "-c", waiter, str(os.getpid()), *launch_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise UpdateError("the updated application could not be scheduled to relaunch") from exc
