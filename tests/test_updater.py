import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mwb_linux.updater import (
    PACKAGE_NAME,
    UpdateError,
    UpdateRelease,
    check_for_update,
    download_release,
    install_package,
    schedule_relaunch,
    version_key,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def release_payload(version: str, content: bytes) -> dict[str, object]:
    name = f"{PACKAGE_NAME}_{version}_amd64.deb"
    return {
        "tag_name": f"v{version}",
        "html_url": (
            "https://github.com/NaveDanan/Mouse-Without-Borders-for-Linux/"
            f"releases/tag/v{version}"
        ),
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    "https://github.com/NaveDanan/Mouse-Without-Borders-for-Linux/"
                    f"releases/download/v{version}/{name}"
                ),
                "size": len(content),
                "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
        ],
    }


class UpdateCheckTests(unittest.TestCase):
    def test_stable_versions_compare_numerically(self):
        self.assertGreater(version_key("v0.10.0"), version_key("0.9.9"))
        with self.assertRaises(UpdateError):
            version_key("0.4")

    def test_new_release_selects_the_matching_architecture_package(self):
        content = b"!<arch>\npackage"
        payload = release_payload("0.4.0", content)
        opener = Mock(return_value=Response(json.dumps(payload).encode()))

        release = check_for_update("0.3.1", architecture="amd64", opener=opener)

        self.assertIsNotNone(release)
        self.assertEqual(release.version, "0.4.0")
        self.assertEqual(release.asset_size, len(content))
        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/NaveDanan/"
            "Mouse-Without-Borders-for-Linux/releases/latest",
        )

    def test_current_or_older_release_returns_none(self):
        payload = release_payload("0.3.1", b"!<arch>\npackage")
        opener = Mock(return_value=Response(json.dumps(payload).encode()))
        self.assertIsNone(
            check_for_update("0.3.1", architecture="amd64", opener=opener)
        )

    def test_release_without_a_github_digest_is_rejected(self):
        payload = release_payload("0.4.0", b"!<arch>\npackage")
        payload["assets"][0]["digest"] = None
        opener = Mock(return_value=Response(json.dumps(payload).encode()))
        with self.assertRaisesRegex(UpdateError, "SHA-256"):
            check_for_update("0.3.1", architecture="amd64", opener=opener)

    def test_network_failure_becomes_an_update_error(self):
        opener = Mock(side_effect=OSError("offline"))
        with self.assertRaisesRegex(UpdateError, "contact GitHub"):
            check_for_update("0.3.1", architecture="amd64", opener=opener)


class UpdateDownloadTests(unittest.TestCase):
    def _release(self, content: bytes, *, digest: str | None = None) -> UpdateRelease:
        return UpdateRelease(
            version="0.4.0",
            tag="v0.4.0",
            page_url="https://example.invalid/release",
            asset_name=f"{PACKAGE_NAME}_0.4.0_amd64.deb",
            asset_url=(
                "https://github.com/NaveDanan/Mouse-Without-Borders-for-Linux/"
                f"releases/download/v0.4.0/{PACKAGE_NAME}_0.4.0_amd64.deb"
            ),
            asset_size=len(content),
            sha256=digest or hashlib.sha256(content).hexdigest(),
        )

    def test_verified_debian_package_is_saved_atomically(self):
        content = b"!<arch>\nverified package bytes"
        with tempfile.TemporaryDirectory() as directory:
            destination = download_release(
                self._release(content),
                directory=Path(directory),
                opener=Mock(return_value=Response(content)),
            )
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_digest_mismatch_removes_partial_download(self):
        content = b"!<arch>\ncorrupt package bytes"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(UpdateError, "SHA-256"):
                download_release(
                    self._release(content, digest="0" * 64),
                    directory=Path(directory),
                    opener=Mock(return_value=Response(content)),
                )
            self.assertEqual(list(Path(directory).iterdir()), [])


class UpdateInstallTests(unittest.TestCase):
    def test_installer_uses_policykit_then_verifies_the_installed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "update.deb"
            package.touch()
            runner = Mock(
                side_effect=[
                    subprocess.CompletedProcess([], 0, stdout="installed"),
                    subprocess.CompletedProcess([], 0, stdout="0.4.0"),
                ]
            )
            with (
                patch("mwb_linux.updater.os.geteuid", return_value=1000),
                patch(
                    "mwb_linux.updater.shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
            ):
                install_package(package, "0.4.0", runner=runner)

        install_command = runner.call_args_list[0].args[0]
        self.assertEqual(
            install_command[:4],
            ["/usr/bin/pkexec", "/usr/bin/apt-get", "install", "--yes"],
        )
        self.assertEqual(
            runner.call_args_list[1].args[0],
            ["/usr/bin/dpkg-query", "--show", "--showformat=${Version}", PACKAGE_NAME],
        )

    def test_failed_or_cancelled_installer_does_not_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "update.deb"
            package.touch()
            runner = Mock(return_value=subprocess.CompletedProcess([], 126, stdout=""))
            with (
                patch("mwb_linux.updater.os.geteuid", return_value=0),
                patch("mwb_linux.updater.shutil.which", return_value="/usr/bin/apt-get"),
            ):
                with self.assertRaisesRegex(UpdateError, "not installed"):
                    install_package(package, "0.4.0", runner=runner)
        self.assertEqual(runner.call_count, 1)

    def test_relaunch_helper_is_detached_and_receives_the_launch_command(self):
        child = SimpleNamespace(pid=123)
        with patch("mwb_linux.updater.subprocess.Popen", return_value=child) as popen:
            self.assertIs(schedule_relaunch(["/usr/bin/example", "ui"]), child)
        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[-2:], ["/usr/bin/example", "ui"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
