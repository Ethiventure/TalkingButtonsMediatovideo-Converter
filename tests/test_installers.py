"""Static contract tests for the native prerequisite launchers.

The launchers must delegate version policy to scripts/check_runtime.py (and,
for FFmpeg, to run_app.py --check-video-tools) instead of embedding thresholds
of their own. They must also prefer an adjacent packaged build and install or
upgrade old runtime components instead of relying on a no-op install.
"""

from __future__ import annotations

import unittest
from pathlib import Path


class InstallerContractTests(unittest.TestCase):
    """Ensure both platforms keep their probe, install and error contracts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.windows = (cls.root / "install_windows.ps1").read_text(encoding="utf-8")
        cls.macos = (cls.root / "install_macos.sh").read_text(encoding="utf-8")

    def test_macos_probes_through_the_checker_and_installs_versioned_formula(self) -> None:
        self.assertIn("check_runtime.py", self.macos)
        self.assertIn("--check-runtime", self.macos)
        self.assertIn("--print-requirements", self.macos)
        self.assertIn("python-tk@3.14", self.macos)
        self.assertIn("brew upgrade python@3.14", self.macos)
        self.assertIn("brew upgrade python-tk@3.14", self.macos)
        self.assertIn("python3.14", self.macos)
        self.assertIn("Frameworks/Python.framework/Versions/3.14", self.macos)
        self.assertIn("--check-video-tools", self.macos)
        self.assertIn("brew upgrade ffmpeg", self.macos)
        self.assertIn("Stage:", self.macos)
        self.assertIn("Problem:", self.macos)
        self.assertIn("What to do:", self.macos)

    def test_macos_prefers_a_packaged_self_contained_build(self) -> None:
        self.assertIn("installer_packaged_app_path", self.macos)
        self.assertIn("Mediatovideo Converter.app", self.macos)
        self.assertIn("dist/Mediatovideo Converter", self.macos)

    def test_windows_probes_through_the_checker_and_installs_latest_314(self) -> None:
        self.assertIn("check_runtime.py", self.windows)
        self.assertIn("--check-runtime", self.windows)
        self.assertIn("--python-arg=", self.windows)
        self.assertIn("--check-video-tools", self.windows)
        self.assertIn("9NQ7512CXL7T", self.windows)
        self.assertIn("install 3.14", self.windows)
        self.assertIn("Gyan.FFmpeg", self.windows)
        self.assertIn("winget upgrade --id Gyan.FFmpeg", self.windows)
        self.assertIn("Stage:", self.windows)
        self.assertIn("Problem:", self.windows)
        self.assertIn("What to do:", self.windows)

    def test_windows_prefers_a_packaged_self_contained_build(self) -> None:
        self.assertIn("Find-PackagedApp", self.windows)
        self.assertIn(r"dist\Mediatovideo Converter.exe", self.windows)

    def test_windows_skips_prerequisites_when_packaged_app_exists(self) -> None:
        startup = self.windows.rsplit('Write-InstallerHeader\n', 1)[1]
        self.assertIn('if (-not (Find-PackagedApp)) {\n    Install-WindowsPrerequisites\n}', startup)
        self.assertIn('winget list --id Gyan.FFmpeg', self.windows)

    def test_no_duplicated_version_policy_in_the_launchers(self) -> None:
        # The old "any Python 3.9 + tkinter import" shortcut must be gone; policy
        # now lives only in mediatovideo_converter/runtime.py.
        self.assertNotIn("sys.version_info >= (3, 9)", self.macos)
        self.assertNotIn("sys.version_info >= (3, 9)", self.windows)
        self.assertNotIn("import sys, tkinter", self.macos)
        self.assertNotIn("import sys, tkinter", self.windows)

    def test_run_launchers_call_native_installers(self) -> None:
        windows_launcher = (self.root / "run_windows.bat").read_text(encoding="utf-8")
        macos_launcher = (self.root / "run_macos.command").read_text(encoding="utf-8")
        self.assertIn("install_windows.ps1", windows_launcher)
        self.assertIn("install_macos.sh", macos_launcher)
        self.assertIn("Press Return", macos_launcher)
        self.assertIn("pause", windows_launcher.casefold())


if __name__ == "__main__":
    unittest.main()
