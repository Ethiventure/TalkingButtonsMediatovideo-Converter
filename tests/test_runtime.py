"""Tests for runtime compatibility policy, probes and the launcher preflight.

These tests stay isolated from the GUI and from the evolving converter module:
they exercise ``mediatovideo_converter.runtime`` and ``run_app`` only, using fake
candidate interpreters so no real Tk window is ever opened here.
"""

from __future__ import annotations

import importlib.abc
import contextlib
import ctypes
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock
from pathlib import Path
from typing import Optional

from mediatovideo_converter import diagnostics, runtime

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


def fake_recovery_module(
    offer: bool = True,
    result: Optional[dict] = None,
    start_error: Optional[BaseException] = None,
    target: Optional[str] = "recovery-target",
) -> types.ModuleType:
    """Build an isolated stand-in for ``mediatovideo_converter.recovery``."""
    module = types.ModuleType("mediatovideo_converter.recovery")
    module.recovery_can_offer = mock.MagicMock(return_value=offer)
    module.recovery_start = mock.MagicMock(
        side_effect=start_error,
        return_value=result
        if result is not None
        else {"ok": True, "detail": "restored", "result_path": "/cache/out"},
    )
    if target is not None:
        module.recovery_target_text = mock.MagicMock(return_value=target)
    return module


def fake_open_process(returncode: int = 0) -> mock.MagicMock:
    """Return a stand-in for the ``open`` process that reports a clean exit."""
    process = mock.MagicMock()
    process.poll.return_value = returncode
    return process


def request_directory_from_argv(argv: list) -> Path:
    """Return the request directory carried by the open --env argument."""
    for index, value in enumerate(argv):
        if value == "--env" and index + 1 < len(argv):
            name, _, path = argv[index + 1].partition("=")
            if name == runtime.RUNTIME_DIALOG_HELPER_ENVIRONMENT:
                return Path(path)
    raise AssertionError("open --env request directory missing from {}".format(argv))


def write_helper_choice(argv: list, choice: str, capture: Optional[dict] = None) -> None:
    """Emulate the compiled applet: read the request directory and write a choice."""
    directory = request_directory_from_argv(argv)
    if capture is not None:
        capture["directory"] = directory
        capture["argv"] = list(argv)
        capture["message"] = (directory / "message.txt").read_text(encoding="utf-8")
        capture["mode"] = (directory / "mode.txt").read_text(encoding="utf-8")
    (directory / "result.txt").write_text(choice, encoding="utf-8")


TRICKY_DIALOG_TEXT = (
    'Stage: Checking packaged video tools\nProblem: "quoted" caf\u00e9 \u2014 \u00fcn\u00efcode'
    " \\backslash\\ and\ttab\n"
)


def extract_applescript_handler(source: str, name: str) -> str:
    """Return one handler block from the public helper source."""
    start = source.index("on {}(".format(name))
    end = source.index("end {}".format(name), start) + len("end {}".format(name))
    return source[start:end]


def run_applescript(script: str) -> str:
    """Run a pure-computation AppleScript and return its trimmed stdout."""
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@contextlib.contextmanager
def injected_recovery(module: types.ModuleType):
    """Install a fake recovery module without leaking the package attribute."""
    import mediatovideo_converter

    had_attribute = hasattr(mediatovideo_converter, "recovery")
    previous = getattr(mediatovideo_converter, "recovery", None)
    with mock.patch.dict(sys.modules, {"mediatovideo_converter.recovery": module}):
        setattr(mediatovideo_converter, "recovery", module)
        try:
            yield module
        finally:
            if had_attribute:
                setattr(mediatovideo_converter, "recovery", previous)
            else:
                try:
                    delattr(mediatovideo_converter, "recovery")
                except AttributeError:
                    pass


class RuntimeRepairTests(unittest.TestCase):
    """The popup offers a local automatic repair when recovery allows it."""

    def setUp(self) -> None:
        diagnostics.diagnostics_shutdown()
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._patches = [
            mock.patch.object(
                diagnostics,
                "_diagnostics_standard_log_path",
                return_value=self.root / "application-debug.log",
            ),
            mock.patch.object(
                diagnostics,
                "_diagnostics_fallback_log_path",
                return_value=self.root / "fallback" / "application-debug.log",
            ),
        ]
        for patch in self._patches:
            patch.start()
        # Default to the legacy path; applet-specific tests re-patch this.
        self._helper_patch = mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=None)
        self._helper_patch.start()
        self._stderr = mock.patch.object(sys, "stderr", io.StringIO())
        self._stderr.start()
        sys.modules.pop("mediatovideo_converter.recovery", None)
        import mediatovideo_converter

        if hasattr(mediatovideo_converter, "recovery"):
            delattr(mediatovideo_converter, "recovery")

    def tearDown(self) -> None:
        diagnostics.diagnostics_shutdown()
        self._helper_patch.stop()
        self._stderr.stop()
        for patch in reversed(self._patches):
            patch.stop()
        self._temporary.cleanup()

    @contextlib.contextmanager
    def without_helper_stub(self):
        """Exercise the real helper lookup for path-resolution tests."""
        self._helper_patch.stop()
        try:
            yield
        finally:
            self._helper_patch.start()

    def test_macos_repair_dialog_returns_true_for_fix(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="Fix automatically\n", stderr="")
        with mock.patch.object(runtime.sys, "platform", "darwin"), mock.patch.object(
            runtime.subprocess, "run", return_value=completed
        ) as run:
            self.assertIs(runtime.runtime_show_repair_dialog("failure body"), True)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[1], "-e")
        self.assertEqual(argv[2], runtime.RUNTIME_APPLESCRIPT_REPAIR_DIALOG)
        self.assertEqual(argv[3], "failure body")

    def test_macos_repair_dialog_returns_false_for_close_or_cancel(self) -> None:
        cancelled = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="execution error: User canceled. (-128)"
        )
        with mock.patch.object(runtime.sys, "platform", "darwin"), mock.patch.object(
            runtime.subprocess, "run", return_value=cancelled
        ):
            self.assertIs(runtime.runtime_show_repair_dialog("failure body"), False)

    def test_macos_repair_dialog_returns_none_when_osascript_is_unavailable(self) -> None:
        with mock.patch.object(runtime.sys, "platform", "darwin"), mock.patch.object(
            runtime.subprocess, "run", side_effect=OSError("no osascript")
        ):
            self.assertIsNone(runtime.runtime_show_repair_dialog("failure body"))

    def test_windows_repair_dialog_maps_yes_and_no(self) -> None:
        for answer, expected in ((6, True), (7, False)):
            fake_ctypes = types.ModuleType("ctypes")
            fake_ctypes.windll = mock.MagicMock()
            fake_ctypes.windll.user32.MessageBoxW.return_value = answer
            with mock.patch.object(runtime.sys, "platform", "win32"), mock.patch.dict(
                sys.modules, {"ctypes": fake_ctypes}
            ):
                self.assertIs(runtime.runtime_show_repair_dialog("failure body"), expected)
            message = fake_ctypes.windll.user32.MessageBoxW.call_args[0][1]
            self.assertIn("Yes", message)
            self.assertIn("fix automatically", message.casefold())

    def test_not_offered_without_a_recovery_module(self) -> None:
        with mock.patch.object(runtime, "runtime_show_repair_dialog") as dialog:
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "not-offered")
        dialog.assert_not_called()

    def test_not_offered_when_can_offer_declines(self) -> None:
        module = fake_recovery_module(offer=False)
        with injected_recovery(module), mock.patch.object(runtime, "runtime_show_repair_dialog") as dialog:
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "not-offered")
        dialog.assert_not_called()
        module.recovery_start.assert_not_called()

    def test_can_offer_receives_the_original_failure_text(self) -> None:
        module = fake_recovery_module(offer=False)
        original = "Stage: Checking packaged video tools\n\nProblem: missing tools"
        with injected_recovery(module):
            runtime.runtime_repair_outcome(original)
        module.recovery_can_offer.assert_called_once_with(original)
        self.assertNotIn("Recovery advice:", module.recovery_can_offer.call_args[0][0])

    def test_can_offer_error_is_treated_as_no_offer(self) -> None:
        module = fake_recovery_module()
        module.recovery_can_offer.side_effect = RuntimeError("policy broken")
        with injected_recovery(module), mock.patch.object(runtime, "runtime_show_repair_dialog") as dialog:
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "not-offered")
        dialog.assert_not_called()

    def test_declined_when_the_user_closes(self) -> None:
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=False):
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "declined")
        module.recovery_start.assert_not_called()

    def test_dialog_unavailable_returns_unavailable(self) -> None:
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=None):
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "unavailable")
        module.recovery_start.assert_not_called()

    def test_repairs_when_the_user_accepts(self) -> None:
        module = fake_recovery_module(
            offer=True, result={"ok": True, "detail": "restored", "result_path": "/cache/out"}
        )
        with injected_recovery(module), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=True):
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "repair-started")
        module.recovery_start.assert_called_once_with(relaunch=True)

    def test_failed_repair_shows_the_no_repair_notice_once(self) -> None:
        module = fake_recovery_module(offer=True, result={"ok": False, "detail": "cache missing"})
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_show_repair_dialog", return_value=True
        ) as dialog, mock.patch.object(runtime, "runtime_show_repair_failure") as notice:
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "failed")
        dialog.assert_called_once()
        module.recovery_start.assert_called_once_with(relaunch=True)
        notice.assert_called_once()
        self.assertIn("cache missing", notice.call_args[0][0])

    def test_raised_repair_error_is_reported_once(self) -> None:
        module = fake_recovery_module(offer=True, start_error=RuntimeError("boom"))
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_show_repair_dialog", return_value=True
        ), mock.patch.object(runtime, "runtime_show_repair_failure") as notice:
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "failed")
        notice.assert_called_once()
        self.assertIn("RuntimeError: boom", notice.call_args[0][0])

    def test_target_text_prefers_the_recovery_getter(self) -> None:
        module = fake_recovery_module(target="/private/tmp/damaged/Mediatovideo Converter.app")
        self.assertEqual(
            runtime.runtime_repair_target_text(module),
            "/private/tmp/damaged/Mediatovideo Converter.app",
        )

    def test_empty_target_text_is_respected_as_no_packaged_target(self) -> None:
        module = fake_recovery_module(target="")
        with mock.patch.object(runtime, "runtime_frozen_app_path") as fallback:
            self.assertEqual(runtime.runtime_repair_target_text(module), "this application copy")
        fallback.assert_not_called()

    def test_target_text_falls_back_when_the_getter_is_missing(self) -> None:
        module = fake_recovery_module(target=None)
        with mock.patch.object(runtime, "runtime_frozen_app_path", return_value=Path("/tmp/fallback.app")):
            self.assertEqual(runtime.runtime_repair_target_text(module), "/tmp/fallback.app")

    def test_frozen_app_path_targets_the_running_bundle(self) -> None:
        mac_executable = "/private/tmp/mediatovideo-damaged/Mediatovideo Converter.app/Contents/MacOS/app"
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=True), mock.patch.object(
            runtime.sys, "executable", mac_executable
        ), mock.patch.object(runtime.sys, "platform", "darwin"):
            self.assertEqual(
                str(runtime.runtime_frozen_app_path()),
                "/private/tmp/mediatovideo-damaged/Mediatovideo Converter.app",
            )
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=True), mock.patch.object(
            runtime.sys, "executable", "/tmp/win/Mediatovideo Converter.exe"
        ), mock.patch.object(runtime.sys, "platform", "win32"):
            self.assertEqual(
                str(runtime.runtime_frozen_app_path()),
                str(Path("/tmp/win/Mediatovideo Converter.exe").resolve()),
            )
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=False):
            self.assertIsNone(runtime.runtime_frozen_app_path())

    def test_report_failure_offers_repair_before_the_fallback_popup(self) -> None:
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=False) as repair_dialog, mock.patch.object(
            runtime, "runtime_show_failure_dialog"
        ) as fallback:
            runtime.runtime_report_failure("Stage: Checking packaged video tools\n\nProblem: missing")
        repair_dialog.assert_called_once()
        fallback.assert_not_called()

    def test_report_failure_keeps_fire_and_forget_when_not_offerable(self) -> None:
        module = fake_recovery_module(offer=False)
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_show_repair_dialog") as repair_dialog, mock.patch.object(
            runtime, "runtime_show_failure_dialog"
        ) as fallback:
            runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        repair_dialog.assert_not_called()
        fallback.assert_called_once()

    def test_report_failure_falls_back_when_the_repair_dialog_is_unavailable(self) -> None:
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=None), mock.patch.object(
            runtime, "runtime_show_failure_dialog"
        ) as fallback:
            runtime.runtime_report_failure("Stage: Checking packaged video tools\n\nProblem: missing")
        fallback.assert_called_once()

    def test_report_failure_never_offers_on_a_source_launch(self) -> None:
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=False
        ), mock.patch.object(runtime, "runtime_show_repair_dialog") as repair_dialog, mock.patch.object(
            runtime, "runtime_show_failure_dialog"
        ) as fallback:
            runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        repair_dialog.assert_not_called()
        fallback.assert_not_called()
        module.recovery_can_offer.assert_not_called()

    def test_can_offer_receives_the_original_exception_object(self) -> None:
        module = fake_recovery_module(offer=False)
        error = ValueError("converter failed")
        with injected_recovery(module):
            runtime.runtime_repair_outcome(error)
        module.recovery_can_offer.assert_called_once_with(error)
        self.assertIs(module.recovery_can_offer.call_args[0][0], error)

    # --- compiled reporter applet contract --------------------------------

    def test_dialog_helper_name_and_source_are_constant(self) -> None:
        self.assertEqual(runtime.runtime_dialog_helper_name(), "Mediatovideo Startup Reporter.app")
        source = runtime.runtime_dialog_helper_source()
        # LaunchServices does not deliver --args to an applet, so the applet has
        # no run argv and reads a request directory named by the environment.
        self.assertNotIn("on run argv", source)
        self.assertIn("on run\n", source + "\n")
        self.assertIn("MEDIATOVIDEO_DIALOG_DIR", source)
        self.assertIn("message.txt", source)
        self.assertIn("mode.txt", source)
        self.assertIn("result.txt", source)
        self.assertIn("readRequest(requestDirectory)", source)
        self.assertIn("\u00abclass utf8\u00bb", source)
        self.assertIn("open for access POSIX file resultPath", source)
        self.assertIn('set chosen to "close"', source)
        # Reviewer-verified: "exists (POSIX file ...)" raises -1708, so the
        # source must probe with get info instead.
        self.assertIn("get info for (POSIX file resultPath)", source)
        self.assertIn("on repairChoice(dialogResult)", source)
        self.assertIn("gave up of dialogResult", source)
        self.assertIn(
            "giving up after {}".format(runtime.RUNTIME_DIALOG_HELPER_GIVE_UP_SECONDS), source
        )
        self.assertNotIn("Stage:", source)

    @unittest.skipUnless(
        sys.platform.startswith("darwin") and shutil.which("osacompile"),
        "osacompile is required",
    )
    def test_dialog_helper_source_compiles(self) -> None:
        result = subprocess.run(
            ["/usr/bin/osacompile", "-o", "/dev/null", "-e", runtime.runtime_dialog_helper_source()],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_helper_path_is_none_outside_a_frozen_bundle(self) -> None:
        with self.without_helper_stub(), mock.patch.object(runtime, "runtime_is_frozen", return_value=False):
            self.assertIsNone(runtime.runtime_dialog_helper_path())

    def test_helper_path_resolves_inside_the_running_bundle(self) -> None:
        app = self.root / "Mediatovideo Converter.app"
        helper = app / "Contents" / "Frameworks" / runtime.runtime_dialog_helper_name()
        helper.mkdir(parents=True)
        with self.without_helper_stub(), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_frozen_app_path", return_value=app), mock.patch.object(
            runtime.sys, "platform", "darwin"
        ):
            self.assertEqual(runtime.runtime_dialog_helper_path(), helper)

    def test_helper_choice_reads_repair_result_and_removes_the_request_directory(self) -> None:
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        seen: dict = {}

        def fake_open(argv, **kwargs):
            seen["argv"] = argv
            write_helper_choice(argv, "repair", seen)
            return fake_open_process()

        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=fake_open
        ):
            self.assertEqual(
                runtime.runtime_dialog_choice("body text", allow_repair=True, timeout=5),
                (True, True),
            )
        self.assertEqual(
            seen["argv"],
            [
                "/usr/bin/open",
                "-n",
                "--env",
                "{}={}".format(runtime.RUNTIME_DIALOG_HELPER_ENVIRONMENT, seen["directory"]),
                str(helper),
            ],
        )
        self.assertEqual(seen["message"], "body text")
        self.assertEqual(seen["mode"], "repair")
        self.assertFalse(Path(seen["directory"]).exists())

    def test_helper_choice_close_result_is_false(self) -> None:
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        seen: dict = {}

        def fake_open(argv, **kwargs):
            seen["argv"] = argv
            write_helper_choice(argv, "close", seen)
            return fake_open_process()

        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=fake_open
        ):
            self.assertEqual(
                runtime.runtime_dialog_choice("notice text", allow_repair=False, timeout=5),
                (True, False),
            )
        self.assertEqual(seen["mode"], "close")
        self.assertFalse(Path(seen["directory"]).exists())

    def test_helper_choice_timeout_returns_none_and_removes_the_request_directory(self) -> None:
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        seen: dict = {}

        def fake_open(argv, **kwargs):
            seen["directory"] = request_directory_from_argv(argv)
            return fake_open_process()

        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=fake_open
        ), mock.patch.object(runtime, "runtime_show_repair_dialog") as legacy:
            self.assertEqual(
                runtime.runtime_dialog_choice("body", allow_repair=True, timeout=0.3),
                (True, None),
            )
        legacy.assert_not_called()
        self.assertFalse(seen["directory"].exists())

    def test_dialog_choice_falls_back_to_osascript_without_the_helper(self) -> None:
        with mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=True) as repair:
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=True), (True, True))
        repair.assert_called_once_with("body")
        with mock.patch.object(runtime, "runtime_show_failure_dialog") as notice:
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=False), (True, False))
        notice.assert_called_once_with("body")

    def test_helper_launch_failure_falls_back_to_the_legacy_dialog(self) -> None:
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=OSError("open missing")
        ), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=True) as legacy:
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=True), (True, True))
        legacy.assert_called_once_with("body")

    def test_launch_failure_without_a_legacy_answer_is_not_started(self) -> None:
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=OSError("open missing")
        ), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=None):
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=True), (False, None))

    def test_repair_outcome_can_use_the_helper_applet(self) -> None:
        module = fake_recovery_module(offer=True)
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()

        def fake_open(argv, **kwargs):
            write_helper_choice(argv, "repair")
            return fake_open_process()

        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_dialog_helper_path", return_value=helper
        ), mock.patch.object(runtime.subprocess, "Popen", side_effect=fake_open):
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "repair-started")
        module.recovery_start.assert_called_once_with(relaunch=True)

    def test_report_failure_uses_the_helper_for_the_no_repair_popup(self) -> None:
        module = fake_recovery_module(offer=False)
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        seen: dict = {}

        def fake_open(argv, **kwargs):
            write_helper_choice(argv, "close", seen)
            return fake_open_process()

        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=fake_open
        ), mock.patch.object(runtime, "runtime_show_failure_dialog") as fallback:
            runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        fallback.assert_not_called()
        self.assertEqual(seen["mode"], "close")

    def test_launch_helper_without_a_helper_is_not_started(self) -> None:
        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=None):
            self.assertEqual(runtime.runtime_launch_dialog_helper("body", True), (False, None))

    def test_repair_offer_text_contains_guidance_and_the_log_path(self) -> None:
        module = fake_recovery_module(offer=True)
        captured: dict = {}

        def fake_legacy(text):
            captured["text"] = text
            return False

        failure = "Stage: Checking packaged video tools\n\nProblem: missing tools"
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_show_repair_dialog", side_effect=fake_legacy
        ):
            self.assertEqual(runtime.runtime_repair_outcome(failure), "declined")
        self.assertIn("Problem: missing tools", captured["text"])
        self.assertIn("Recovery advice:", captured["text"])
        self.assertIn("application-debug.log", captured["text"])

    def test_repair_timeout_returns_no_answer(self) -> None:
        module = fake_recovery_module(offer=True)
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()

        def fake_open(argv, **kwargs):
            return fake_open_process()

        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_dialog_helper_path", return_value=helper
        ), mock.patch.object(runtime.subprocess, "Popen", side_effect=fake_open), mock.patch.object(
            runtime, "RUNTIME_DIALOG_HELPER_TIMEOUT_SECONDS", 0.3
        ):
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "no-answer")
        module.recovery_start.assert_not_called()

    def test_report_failure_does_not_duplicate_the_dialog_on_timeout(self) -> None:
        module = fake_recovery_module(offer=True)
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()

        def fake_open(argv, **kwargs):
            return fake_open_process()

        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=fake_open
        ), mock.patch.object(
            runtime, "RUNTIME_DIALOG_HELPER_TIMEOUT_SECONDS", 0.3
        ), mock.patch.object(runtime, "runtime_show_failure_dialog") as fallback, mock.patch.object(
            runtime, "runtime_show_repair_failure"
        ) as notice:
            runtime.runtime_report_failure("Stage: Checking packaged video tools\n\nProblem: missing")
        fallback.assert_not_called()
        notice.assert_not_called()

    def test_actual_dialog_channel_is_logged_in_the_branch_that_ran(self) -> None:
        # The applet branch logs macos-reporter only after a real launch.
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()

        def fake_open(argv, **kwargs):
            write_helper_choice(argv, "close")
            return fake_open_process()

        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", side_effect=fake_open
        ), mock.patch.object(runtime, "runtime_log_info") as log:
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=True), (True, False))
        channels = [call.kwargs.get("channel") for call in log.call_args_list]
        self.assertIn("macos-reporter", channels)

        # The legacy branch logs the channel it actually used, and the outer
        # "Repair requested" line carries no channel claim.
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_show_repair_dialog", return_value=False
        ), mock.patch.object(runtime, "runtime_log_info") as legacy_log:
            self.assertEqual(runtime.runtime_repair_outcome("failure"), "declined")
        requested = [
            call for call in legacy_log.call_args_list if call.args and call.args[0] == "Repair requested"
        ]
        self.assertEqual(requested, [mock.call("Repair requested", target="recovery-target")])
        legacy_channels = [
            call.kwargs.get("channel")
            for call in legacy_log.call_args_list
            if call.args and call.args[0] == "Legacy repair dialog"
        ]
        self.assertEqual(len(legacy_channels), 1)
        self.assertIn(legacy_channels[0], {"macos-osascript", "windows-messagebox", "unsupported"})

    def test_successful_repair_shows_no_redundant_failure_popup(self) -> None:
        module = fake_recovery_module(offer=True)
        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_dialog_choice", return_value=(True, True)) as choice, mock.patch.object(
            runtime, "runtime_show_failure_dialog"
        ) as fallback, mock.patch.object(runtime, "runtime_show_repair_failure") as notice:
            runtime.runtime_report_failure("Stage: Checking packaged video tools\n\nProblem: missing")
        choice.assert_called_once()
        fallback.assert_not_called()
        notice.assert_not_called()
        module.recovery_start.assert_called_once_with(relaunch=True)

    def test_rejected_helper_falls_back_to_the_legacy_dialog(self) -> None:
        helper = self.root / runtime.runtime_dialog_helper_name()
        helper.mkdir()
        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", return_value=fake_open_process(returncode=1)
        ):
            self.assertEqual(
                runtime.runtime_launch_dialog_helper("body", True, timeout=5), (False, None)
            )
        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=helper), mock.patch.object(
            runtime.subprocess, "Popen", return_value=fake_open_process(returncode=1)
        ), mock.patch.object(runtime, "runtime_show_repair_dialog", return_value=True) as legacy:
            self.assertEqual(
                runtime.runtime_dialog_choice("body", allow_repair=True, timeout=5), (True, True)
            )
        legacy.assert_called_once_with("body")

    @unittest.skipUnless(
        sys.platform.startswith("darwin") and shutil.which("open"),
        "macOS open is required",
    )
    def test_real_open_rejection_is_detected_without_ui(self) -> None:
        # A missing applet makes the real `open` exit nonzero (no window is
        # shown), so the reporter must treat it as a launch failure.
        missing = self.root / "Missing Reporter.app"
        with mock.patch.object(runtime, "runtime_dialog_helper_path", return_value=missing):
            self.assertEqual(
                runtime.runtime_launch_dialog_helper("body", True, timeout=5), (False, None)
            )

    def test_notice_dialog_failure_propagates_not_started(self) -> None:
        with mock.patch.object(runtime, "runtime_show_failure_dialog", return_value=False):
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=False), (False, None))
        with mock.patch.object(runtime, "runtime_show_failure_dialog", return_value=True):
            self.assertEqual(runtime.runtime_dialog_choice("body", allow_repair=False), (True, False))

    def test_give_up_timer_is_shorter_than_the_python_wait(self) -> None:
        self.assertLess(
            runtime.RUNTIME_DIALOG_HELPER_GIVE_UP_SECONDS,
            runtime.RUNTIME_DIALOG_HELPER_TIMEOUT_SECONDS,
        )

    @unittest.skipUnless(
        sys.platform.startswith("darwin") and shutil.which("osascript"),
        "osascript is required",
    )
    def test_repair_choice_handler_ignores_a_gave_up_dialog(self) -> None:
        handler = extract_applescript_handler(runtime.runtime_dialog_helper_source(), "repairChoice")
        script = handler + (
            '\nreturn repairChoice({button returned:"Fix automatically", gave up:true})'
            ' & "," & repairChoice({button returned:"Fix automatically", gave up:false})'
            ' & "," & repairChoice({button returned:"Close", gave up:false})\n'
        )
        self.assertEqual(run_applescript(script), "close,repair,close")

    @unittest.skipUnless(
        sys.platform.startswith("darwin") and shutil.which("osascript"),
        "osascript is required",
    )
    def test_write_result_handler_writes_and_never_recreates_a_missing_file(self) -> None:
        handler = extract_applescript_handler(runtime.runtime_dialog_helper_source(), "writeResult")
        existing = self.root / "result.txt"
        existing.write_text("", encoding="utf-8")
        missing = self.root / "missing-result.txt"
        script = handler + (
            '\nwriteResult("repair", "{existing}")\n'
            'writeResult("repair", "{missing}")\n'
            'return "done"\n'
        ).format(existing=existing.as_posix(), missing=missing.as_posix())
        self.assertEqual(run_applescript(script), "done")
        self.assertEqual(existing.read_text(encoding="utf-8"), "repair")
        self.assertFalse(missing.exists())

    def test_no_offer_popup_explains_manual_install(self) -> None:
        module = fake_recovery_module(offer=False)
        captured: dict = {}

        def fake_notice(text):
            captured["text"] = text
            return True

        with injected_recovery(module), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_show_failure_dialog", side_effect=fake_notice):
            runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        self.assertIn("Automatic repair is not available", captured["text"])
        self.assertIn("fresh download", captured["text"])
        # Source guidance must stay free of the frozen no-offer sentence.
        self.assertNotIn(
            "Automatic repair is not available", runtime.runtime_failure_message("Stage: Runtime")
        )

    def test_windows_dialog_definition_uses_the_exact_labels(self) -> None:
        definition = runtime.runtime_windows_dialog_definition("body text")
        self.assertEqual(
            [(entry["id"], entry["label"]) for entry in definition["buttons"]],
            [
                (runtime.RUNTIME_WINDOWS_CLOSE_BUTTON_ID, "Close"),
                (runtime.RUNTIME_WINDOWS_FIX_BUTTON_ID, "Fix automatically"),
            ],
        )
        self.assertEqual(definition["default_button_id"], runtime.RUNTIME_WINDOWS_FIX_BUTTON_ID)
        self.assertIn("Fix automatically", str(definition["content"]))
        self.assertIn("Close", str(definition["content"]))
        self.assertIn("body text", str(definition["content"]))

    def test_windows_task_dialog_result_maps_to_fix_and_close(self) -> None:
        for answer, expected in ((True, True), (False, False)):
            with mock.patch.object(runtime.sys, "platform", "win32"), mock.patch.object(
                runtime, "runtime_windows_task_dialog", return_value=answer
            ) as task_dialog, mock.patch.object(runtime, "runtime_windows_message_box") as message_box:
                self.assertIs(runtime.runtime_show_repair_dialog("body"), expected)
            task_dialog.assert_called_once_with("body")
            message_box.assert_not_called()

    def test_windows_task_dialog_unavailable_falls_back_to_the_message_box(self) -> None:
        for fallback, expected in ((True, True), (False, False), (None, None)):
            with mock.patch.object(runtime.sys, "platform", "win32"), mock.patch.object(
                runtime, "runtime_windows_task_dialog", return_value=None
            ), mock.patch.object(
                runtime, "runtime_windows_message_box", return_value=fallback
            ) as message_box:
                self.assertIs(runtime.runtime_show_repair_dialog("body"), expected)
            message_box.assert_called_once_with("body")

    def test_windows_task_dialog_is_gracefully_unavailable_off_windows(self) -> None:
        # The real function must return None (never raise) when comctl32 or the
        # ctypes ABI is unavailable, so the caller can fall back.
        with mock.patch.object(runtime.sys, "platform", "win32"):
            self.assertIsNone(runtime.runtime_windows_task_dialog("body"))

    @unittest.skipUnless(sys.platform.startswith("win"), "native Windows TaskDialog")
    def test_windows_task_dialog_native_autoclick_selects_the_expected_button(self) -> None:
        # Real ctypes ABI exercise: the TDN_CREATED callback posts
        # TDM_CLICK_BUTTON so the dialog closes itself without user input.
        results: dict = {}

        def run_case(button_id: int, key: str) -> None:
            with mock.patch.object(runtime, "RUNTIME_WINDOWS_TASK_DIALOG_AUTOCLICK_ID", button_id):
                results[key] = runtime.runtime_windows_task_dialog("native smoke")

        for button_id, key in (
            (runtime.RUNTIME_WINDOWS_FIX_BUTTON_ID, "fix"),
            (runtime.RUNTIME_WINDOWS_CLOSE_BUTTON_ID, "close"),
        ):
            worker = threading.Thread(target=run_case, args=(button_id, key), daemon=True)
            worker.start()
            worker.join(timeout=60)
            self.assertFalse(
                worker.is_alive(),
                "TaskDialog did not dismiss within the watchdog; the autoclick callback did not fire",
            )
        self.assertIs(results.get("fix"), True)
        self.assertIs(results.get("close"), False)

    def test_windows_task_dialog_layout_matches_commctrl(self) -> None:
        structures = runtime.runtime_windows_task_dialog_structures()
        self.assertIsNotNone(structures)
        button, config = structures
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(button), 16 if pointer == 8 else 8)
        if pointer == 8:
            self.assertEqual(ctypes.sizeof(config), 176)
            expected = {
                "cbSize": 0, "hwndParent": 8, "hInstance": 16, "pszWindowTitle": 32,
                "pszMainInstruction": 48, "pszContent": 56, "cButtons": 64, "pButtons": 72,
                "nDefaultButton": 80, "pfCallback": 152, "lpCallbackData": 160, "cxWidth": 168,
            }
        else:
            self.assertEqual(ctypes.sizeof(config), 96)
            expected = {
                "cbSize": 0, "hwndParent": 4, "hInstance": 8, "pszWindowTitle": 20,
                "pszMainInstruction": 28, "pszContent": 32, "cButtons": 36, "pButtons": 40,
                "nDefaultButton": 44, "pfCallback": 84, "lpCallbackData": 88, "cxWidth": 92,
            }
        for name, offset in expected.items():
            self.assertEqual(getattr(config, name).offset, offset, name)

    def test_windows_post_dialog_click_is_gracefully_unavailable_off_windows(self) -> None:
        with mock.patch.object(runtime.sys, "platform", "win32"):
            self.assertFalse(
                runtime.runtime_windows_post_dialog_click(0, runtime.RUNTIME_WINDOWS_FIX_BUTTON_ID)
            )

    @unittest.skipUnless(
        sys.platform.startswith("darwin") and shutil.which("osacompile") and shutil.which("open"),
        "compiled applet transport requires macOS",
    )
    def test_compiled_applet_env_transport_preserves_utf8_quotes_and_newlines(self) -> None:
        # Real LaunchServices transport: compile a probe that reuses the
        # production readRequest handler, launch it with open -n --env, and
        # prove the applet reads the exact UTF-8 request text.
        read_handler = extract_applescript_handler(runtime.runtime_dialog_helper_source(), "readRequest")
        driver = (
            "on run\n"
            'set requestDirectory to ""\n'
            "try\n"
            'set requestDirectory to (system attribute "'
            + runtime.RUNTIME_DIALOG_HELPER_ENVIRONMENT
            + '") as text\n'
            "end try\n"
            "set values to readRequest(requestDirectory)\n"
            'writeUtf8(item 1 of values, requestDirectory & "/echo.txt")\n'
            "return \"done\"\n"
            "end run\n"
            "\n"
            "on writeUtf8(theText, resultPath)\n"
            "set resultFileExists to false\n"
            "try\n"
            "get info for (POSIX file resultPath)\n"
            "set resultFileExists to true\n"
            "on error\n"
            "set resultFileExists to false\n"
            "end try\n"
            "if resultFileExists then\n"
            "set fileHandle to open for access POSIX file resultPath with write permission\n"
            "set eof of fileHandle to 0\n"
            "write theText to fileHandle as \u00abclass utf8\u00bb\n"
            "close access fileHandle\n"
            "end if\n"
            "end writeUtf8\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            source = fixture / "probe.applescript"
            app = fixture / "probe.app"
            source.write_text(read_handler + "\n" + driver, encoding="utf-8")
            compiled = subprocess.run(
                ["/usr/bin/osacompile", "-o", str(app), str(source)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            request = fixture / "request's folder"
            request.mkdir()
            (request / "message.txt").write_text(TRICKY_DIALOG_TEXT, encoding="utf-8")
            (request / "mode.txt").write_text("close", encoding="utf-8")
            echo = request / "echo.txt"
            echo.write_text("", encoding="utf-8")
            launched = subprocess.run(
                [
                    "/usr/bin/open",
                    "-n",
                    "--env",
                    "{}={}".format(runtime.RUNTIME_DIALOG_HELPER_ENVIRONMENT, request),
                    str(app),
                ],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not echo.read_text(encoding="utf-8"):
                time.sleep(0.2)
            self.assertEqual(echo.read_text(encoding="utf-8"), TRICKY_DIALOG_TEXT)


if __name__ == "__main__":
    unittest.main()
