"""Tests for runtime compatibility policy, probes and the launcher preflight.

These tests stay isolated from the GUI and from the evolving converter module:
they exercise ``mediatovideo_converter.runtime`` and ``run_app`` only, using fake
candidate interpreters so no real Tk window is ever opened here.
"""

from __future__ import annotations

import importlib.abc
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock
from pathlib import Path

from mediatovideo_converter import runtime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RUN_APP = ROOT / "run_app.py"
CHECK_RUNTIME = ROOT / "scripts" / "check_runtime.py"
OLD_PYTHON = Path("/usr/bin/python3")

OK_PAYLOAD = {
    "ok": True,
    "python_version": "3.14.8",
    "python_executable": "/fake/python3.14",
    "platform": "darwin",
    "tk_patchlevel": "9.1.0",
    "tk_version": "9.1",
    "widgets_ok": True,
}

FAKE_CANDIDATE = """#!/bin/sh
# Fake candidate interpreter: writes the same JSON report the real probe writes.
report=""
previous=""
for argument in "$@"; do
    if [ "$previous" = "--runtime-probe-report" ]; then report=$argument; fi
    previous=$argument
done
if [ -n "$report" ]; then
    printf '%s' '{payload}' > "$report"
fi
exit {exit_code}
"""


def write_fake(directory: Path, name: str, payload: dict, exit_code: int = 0) -> Path:
    """Create an executable fake candidate interpreter for one probe verdict."""
    path = directory / name
    path.write_text(
        FAKE_CANDIDATE.replace("{payload}", json.dumps(payload)).replace(
            "{exit_code}", str(exit_code)
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class RuntimePolicyTests(unittest.TestCase):
    """Minimum versions live in one module and are compared correctly."""

    def test_platform_requirements(self) -> None:
        macos = runtime.runtime_get_requirements("darwin")
        windows = runtime.runtime_get_requirements("win32")
        other = runtime.runtime_get_requirements("linux")

        self.assertEqual(macos["python"], (3, 14, 8))
        self.assertEqual(macos["tk"], (9, 1, 0))
        self.assertEqual(windows["tk"], (9, 0, 4))
        self.assertEqual(other["tk"], (8, 6, 0))
        self.assertEqual(macos["python_text"], "3.14.8")
        self.assertEqual(macos["tk_text"], "9.1.0")

    def test_python_minimum_boundaries(self) -> None:
        minimum = runtime.RUNTIME_MIN_PYTHON
        self.assertTrue(runtime.runtime_version_supported("3.14.8", minimum))
        self.assertTrue(runtime.runtime_version_supported((3, 14, 8), minimum))
        self.assertFalse(runtime.runtime_version_supported("3.14.8rc1", minimum))
        self.assertTrue(runtime.runtime_version_supported("3.15.0", minimum))
        self.assertTrue(runtime.runtime_version_supported("4.0.0", minimum))
        self.assertFalse(runtime.runtime_version_supported("3.14.7", minimum))
        self.assertFalse(runtime.runtime_version_supported("3.9.6", minimum))
        self.assertFalse(runtime.runtime_version_supported("", minimum))

    def test_tk_minimum_boundaries_are_platform_specific(self) -> None:
        self.assertTrue(runtime.runtime_version_supported("9.1.0", runtime.RUNTIME_MIN_TK_MACOS))
        self.assertTrue(runtime.runtime_version_supported("9.1", runtime.RUNTIME_MIN_TK_MACOS))
        self.assertFalse(runtime.runtime_version_supported("9.0.4", runtime.RUNTIME_MIN_TK_MACOS))
        self.assertTrue(runtime.runtime_version_supported("9.0.4", runtime.RUNTIME_MIN_TK_WINDOWS))
        self.assertFalse(runtime.runtime_version_supported("9.0.3", runtime.RUNTIME_MIN_TK_WINDOWS))
        self.assertFalse(runtime.runtime_version_supported("8.6.14", runtime.RUNTIME_MIN_TK_WINDOWS))


@unittest.skipIf(os.name == "nt", "POSIX fake candidate scripts; native frozen probe is exercised in CI")
class RuntimeProbeTests(unittest.TestCase):
    """Supervised probes accept good candidates and reject every bad shape."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_supported_candidate_is_accepted(self) -> None:
        candidate = write_fake(self.directory, "fake-new-python", OK_PAYLOAD)
        payload = runtime.runtime_check(str(candidate), timeout=20)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tk_patchlevel"], "9.1.0")
        self.assertTrue(payload["widgets_ok"])

    def test_report_file_alone_is_enough_when_stdout_is_empty(self) -> None:
        # Regression: a frozen windowed child can have no stdout at all. The
        # fake writes only the JSON report file and prints nothing.
        candidate = write_fake(self.directory, "fake-no-stdout", OK_PAYLOAD)
        payload = runtime.runtime_check(str(candidate), timeout=20)
        self.assertTrue(payload["ok"])

    def test_old_python_candidate_is_rejected_with_python_stage(self) -> None:
        candidate = write_fake(
            self.directory,
            "fake-old-python",
            {"ok": False, "stage": "Python version", "problem": "Python 3.9.6 is older.",
             "action": "Install the launcher runtime.", "details": ""},
            exit_code=1,
        )
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(candidate), timeout=20)
        self.assertEqual(caught.exception.stage, "Python version")
        self.assertIn("What to do:", str(caught.exception))

    def test_old_tk_candidate_is_rejected_with_tk_stage(self) -> None:
        candidate = write_fake(
            self.directory,
            "fake-old-tk",
            {"ok": False, "stage": "Tk version", "problem": "Tk 8.5 is older.",
             "action": "Install Tk 9.1 or newer.", "details": "Tk patchlevel: 8.5.9"},
            exit_code=1,
        )
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(candidate), timeout=20)
        self.assertEqual(caught.exception.stage, "Tk version")
        self.assertIn("8.5.9", caught.exception.details)

    def test_missing_widget_support_is_rejected(self) -> None:
        candidate = write_fake(
            self.directory,
            "fake-no-widgets",
            {"ok": False, "stage": "GUI widgets", "problem": "Treeview did not draw.",
             "action": "Install the Tk drawing kit.", "details": "TclError"},
            exit_code=1,
        )
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(candidate), timeout=20)
        self.assertEqual(caught.exception.stage, "GUI widgets")

    def test_candidate_without_a_report_is_rejected_clearly(self) -> None:
        silent = self.directory / "fake-silent-python"
        silent.write_text("#!/bin/sh\necho 'no report here' >&2\nexit 3\n", encoding="utf-8")
        silent.chmod(0o755)
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(silent), timeout=20)
        self.assertEqual(caught.exception.stage, "Checking Python and Tkinter")
        self.assertIn("no report here", caught.exception.details)

    def test_hanging_candidate_is_killed_by_the_watchdog(self) -> None:
        hanging = self.directory / "fake-hanging-python"
        hanging.write_text("#!/bin/sh\nsleep 120\n", encoding="utf-8")
        hanging.chmod(0o755)
        started = time.monotonic()
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(hanging), timeout=2)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.stage, "Runtime check timed out")
        self.assertLess(elapsed, 15.0)
        if subprocess.run(["sh", "-c", "command -v pgrep"], capture_output=True).returncode == 0:
            leftover = subprocess.run(["pgrep", "-f", "fake-hanging-python"], capture_output=True, text=True)
            self.assertEqual(leftover.stdout.strip(), "", "watchdog left the probe running")

    def test_missing_candidate_reports_spawn_failure(self) -> None:
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(self.directory / "does-not-exist"), timeout=5)
        self.assertEqual(caught.exception.stage, "Starting Python")


class FrozenTransportTests(unittest.TestCase):
    """The report file works when there is no stdout at all."""

    def test_probe_main_writes_report_with_stdout_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "probe.json"
            original_stdout = sys.stdout
            sys.stdout = None
            try:
                code = runtime.runtime_probe_main(
                    [runtime.RUNTIME_PROBE_REPORT_FLAG, str(report)]
                )
            finally:
                sys.stdout = original_stdout
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertIsInstance(payload["ok"], bool)
            self.assertEqual(code, 0 if payload["ok"] else 1)


@unittest.skipUnless(
    OLD_PYTHON.exists() and sys.version_info < (3, 14, 8),
    "runs with the real old interpreter",
)
class OldInterpreterTests(unittest.TestCase):
    """The real old interpreter is rejected before Tk is ever touched."""

    def isolated_environment(self) -> dict:
        """Return an environment whose diagnostic log lands in a temp directory."""
        home = tempfile.mkdtemp(prefix="mediatovideo-test-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        environment = dict(os.environ)
        environment["HOME"] = home
        environment["XDG_STATE_HOME"] = os.path.join(home, "state")
        environment["LOCALAPPDATA"] = os.path.join(home, "localappdata")
        return environment

    def test_version_gate_stops_the_probe_before_tkinter(self) -> None:
        class PoisonFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):  # type: ignore[no-untyped-def]
                if fullname == "tkinter":
                    raise AssertionError("Tkinter must not be imported on an old Python")
                return None

        finder = PoisonFinder()
        sys.meta_path.insert(0, finder)
        try:
            payload = runtime.runtime_probe_execute()
        finally:
            sys.meta_path.remove(finder)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], "Python version")

    def test_runtime_check_rejects_the_real_old_interpreter(self) -> None:
        with self.assertRaises(runtime.RuntimeUnsupportedError) as caught:
            runtime.runtime_check(str(OLD_PYTHON), timeout=30)
        self.assertEqual(caught.exception.stage, "Python version")
        self.assertIn("3.14.8", caught.exception.problem)

    def test_run_app_cli_rejects_the_real_old_interpreter(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RUN_APP), "--check-runtime"],
            capture_output=True, text=True, timeout=90, env=self.isolated_environment(),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Stage:   Python version", result.stderr)
        self.assertIn("What to do:", result.stderr)

    def test_run_app_runtime_probe_report_works_without_console_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "probe.json"
            result = subprocess.run(
                [sys.executable, str(RUN_APP), "--runtime-probe-report", str(report)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
                env=self.isolated_environment(),
            )
            self.assertEqual(result.returncode, 1)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["stage"], "Python version")


class RunAppPreflightTests(unittest.TestCase):
    """The launcher fails closed before importing the GUI module."""

    def test_gui_module_is_not_imported_when_the_runtime_is_unsupported(self) -> None:
        import run_app  # noqa: PLC0415
        from mediatovideo_converter import diagnostics  # noqa: PLC0415

        sys.modules.pop("mediatovideo_converter.WEB_UI", None)
        error = runtime.RuntimeUnsupportedError("Python version", "Unsupported Python", "Install a supported runtime.")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            diagnostics,
            "_diagnostics_standard_log_path",
            return_value=Path(temporary) / "application-debug.log",
        ), mock.patch.object(
            diagnostics,
            "_diagnostics_fallback_log_path",
            return_value=Path(temporary) / "fallback" / "application-debug.log",
        ), mock.patch.object(runtime, "runtime_check", side_effect=error), redirect_stderr(io.StringIO()):
            try:
                code = run_app.run_app_main([])
            finally:
                diagnostics.diagnostics_shutdown()
        self.assertNotIn("mediatovideo_converter.WEB_UI", sys.modules)
        self.assertEqual(code, 2)

    def test_check_runtime_cli_is_available_from_source(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECK_RUNTIME), "--print-requirements"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("python>=3.14.8", result.stdout)
        self.assertIn("tk>=", result.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX fake candidate script")
    def test_checker_rejects_a_fake_old_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            candidate = write_fake(
                directory,
                "fake-old",
                {"ok": False, "stage": "Tk version", "problem": "Tk 8.5 is older.",
                 "action": "Install Tk 9.1 or newer.", "details": ""},
                exit_code=1,
            )
            result = subprocess.run(
                [sys.executable, str(CHECK_RUNTIME), "--check-runtime", "--python", str(candidate)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Stage:   Tk version", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX fake candidate script")
    def test_checker_accepts_a_supported_fake_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = write_fake(Path(temporary), "fake-new", OK_PAYLOAD)
            result = subprocess.run(
                [sys.executable, str(CHECK_RUNTIME), "--check-runtime", "--python", str(candidate)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK: Compatible Python and Tkinter found", result.stdout)

    def test_log_path_cli_keeps_stdout_clean(self) -> None:
        environment = dict(os.environ)
        home = tempfile.mkdtemp(prefix="mediatovideo-logpath-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        environment["HOME"] = home
        environment["XDG_STATE_HOME"] = os.path.join(home, "state")
        environment["LOCALAPPDATA"] = os.path.join(home, "localappdata")
        result = subprocess.run(
            [sys.executable, str(RUN_APP), "--log-path"],
            capture_output=True, text=True, timeout=90, env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("application-debug.log"), lines[0])
        self.assertIn("Application startup", result.stderr)


if __name__ == "__main__":
    unittest.main()
