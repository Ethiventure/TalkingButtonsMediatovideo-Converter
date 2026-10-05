"""Tests for the persistent rotating diagnostic log and runtime failure text.

These tests exercise real files in temporary directories (no mocking of the log
itself), never open a native viewer or dialog, and stay isolated from the GUI.
"""

from __future__ import annotations

import io
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from mediatovideo_converter import diagnostics, runtime

ROOT = Path(__file__).resolve().parents[1]

# A failure block with quotes, newlines, tabs and backslashes: the native dialog
# must transport it verbatim instead of interpolating it into script source.
DIALOG_FAULT_TEXT = 'Stage: Runtime\nProblem: "quoted" \\backslash\\ and\ttab\nWhat to do: retry'


class DiagnosticsTestCase(unittest.TestCase):
    """Base class that points both log locations at temporary directories."""

    def setUp(self) -> None:
        diagnostics.diagnostics_shutdown()
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.log_path = self.root / "standard" / diagnostics.DIAGNOSTICS_LOG_BASENAME
        self.fallback_path = self.root / "fallback" / diagnostics.DIAGNOSTICS_LOG_BASENAME
        self._patches = [
            mock.patch.object(diagnostics, "_diagnostics_standard_log_path", return_value=self.log_path),
            mock.patch.object(diagnostics, "_diagnostics_fallback_log_path", return_value=self.fallback_path),
        ]
        for patch in self._patches:
            patch.start()
        self.stderr = io.StringIO()
        self._stderr_patch = mock.patch.object(sys, "stderr", self.stderr)
        self._stderr_patch.start()

    def tearDown(self) -> None:
        diagnostics.diagnostics_shutdown()
        self._stderr_patch.stop()
        for patch in reversed(self._patches):
            patch.stop()
        self._temporary.cleanup()

    def logged_text(self, path: Path) -> str:
        """Return the saved log text for assertions."""
        return path.read_text(encoding="utf-8")


class DiagnosticsSessionTests(DiagnosticsTestCase):
    """Session start, idempotency, metadata and field rendering."""

    def test_start_is_idempotent_and_returns_the_active_path(self) -> None:
        first = diagnostics.diagnostics_start()
        second = diagnostics.diagnostics_start()
        self.assertEqual(first, self.log_path)
        self.assertEqual(second, first)
        self.assertEqual(diagnostics.diagnostics_log_path(), first)
        self.assertTrue(first.exists())

    def test_session_metadata_records_version_python_os_frozen_and_requirements(self) -> None:
        diagnostics.diagnostics_start()
        text = self.logged_text(self.log_path)
        self.assertIn("Diagnostic session started", text)
        self.assertIn("version=", text)
        # Assert against the interpreter actually running the suite so the test
        # passes on both the system Python and the private 3.14.8 runtime.
        self.assertIn("python={}".format(platform.python_version()), text)
        self.assertIn("os=", text)
        self.assertIn("frozen=False", text)
        requirements = runtime.runtime_get_requirements()
        self.assertIn(
            "requires=python>={} tk>={}".format(
                requirements["python_text"], requirements["tk_text"]
            ),
            text,
        )
        self.assertIn("INFO", self.stderr.getvalue())

    def test_info_records_message_and_field_values(self) -> None:
        diagnostics.diagnostics_start()
        diagnostics.diagnostics_info("Application startup", version="9.9.9")
        diagnostics.diagnostics_info(
            "Runtime compatibility check passed", python_version="3.14.8", tk_patchlevel="9.1.0"
        )
        # Contract used by the bundle self test: the window-ready marker and the
        # loaded Tk version appear verbatim in the saved log.
        diagnostics.diagnostics_info("Application window initialized", tk="9.1.0")
        text = self.logged_text(self.log_path)
        self.assertIn("Application startup version=9.9.9", text)
        self.assertIn("python_version=3.14.8", text)
        self.assertIn("tk_patchlevel=9.1.0", text)
        self.assertIn("Application window initialized tk=9.1.0", text)

    def test_info_before_start_writes_stderr_only(self) -> None:
        diagnostics.diagnostics_info("Not started yet")
        self.assertIn("Not started yet", self.stderr.getvalue())
        self.assertFalse(self.log_path.exists())

    def test_log_path_is_none_after_shutdown(self) -> None:
        diagnostics.diagnostics_start()
        diagnostics.diagnostics_shutdown()
        self.assertIsNone(diagnostics.diagnostics_log_path())

    def test_large_structured_field_is_bounded(self) -> None:
        diagnostics.diagnostics_start()
        payload = {"key{}".format(index): "value" * 200 for index in range(50)}
        diagnostics.diagnostics_info("Bounded field", payload=payload)
        record = [
            line for line in self.logged_text(self.log_path).splitlines() if "Bounded field" in line
        ]
        self.assertEqual(len(record), 1)
        self.assertLess(len(record[0]), 1500)
        self.assertIn("...", record[0])


class DiagnosticsExceptionTests(DiagnosticsTestCase):
    """Exceptions are serialized with a real traceback."""

    def test_supplied_error_traceback_is_serialized(self) -> None:
        diagnostics.diagnostics_start()
        try:
            raise ValueError("queued failure")
        except ValueError as error:
            captured = error
        diagnostics.diagnostics_exception("Queued error", captured)
        text = self.logged_text(self.log_path)
        self.assertIn("Queued error", text)
        self.assertIn("ValueError: queued failure", text)
        self.assertIn("Traceback (most recent call last)", text)

    def test_current_exc_info_is_used_without_an_error_argument(self) -> None:
        diagnostics.diagnostics_start()
        try:
            raise KeyError("current failure")
        except KeyError:
            diagnostics.diagnostics_exception("Current error")
        text = self.logged_text(self.log_path)
        self.assertIn("Current error", text)
        self.assertIn("KeyError", text)
        self.assertIn("current failure", text)

    def test_exception_without_any_active_error_is_still_recorded(self) -> None:
        diagnostics.diagnostics_start()
        diagnostics.diagnostics_exception("Bare failure")
        self.assertIn("Bare failure", self.logged_text(self.log_path))


class DiagnosticsStreamTests(DiagnosticsTestCase):
    """Frozen windowed builds may have no streams at all."""

    def test_none_streams_are_safe_and_the_file_still_records(self) -> None:
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            path = diagnostics.diagnostics_start()
            diagnostics.diagnostics_info("Frozen start", version="1.0")
            diagnostics.diagnostics_exception("Frozen error")
            diagnostics.diagnostics_shutdown()
        self.assertIsNotNone(path)
        text = self.logged_text(path)
        self.assertIn("Frozen start version=1.0", text)
        self.assertIn("Frozen error", text)

    def test_unavailable_log_with_none_streams_does_not_raise(self) -> None:
        (self.root / "standard").write_text("blocker", encoding="utf-8")
        (self.root / "fallback").write_text("blocker", encoding="utf-8")
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            path = diagnostics.diagnostics_start()
            diagnostics.diagnostics_info("Silently dropped")
        self.assertIsNone(path)
        self.assertIsNone(diagnostics.diagnostics_log_path())


class DiagnosticsFallbackTests(DiagnosticsTestCase):
    """Location policy: standard path, disclosed fallback, then graceful None."""

    def test_unusable_standard_directory_falls_back_to_temp_and_discloses_it(self) -> None:
        (self.root / "standard").write_text("not a directory", encoding="utf-8")
        path = diagnostics.diagnostics_start()
        self.assertEqual(path, self.fallback_path)
        self.assertTrue(path.exists())
        self.assertEqual(diagnostics.diagnostics_log_path(), self.fallback_path)
        text = self.logged_text(path)
        self.assertIn("fallback", text.casefold())
        self.assertIn("fallback", self.stderr.getvalue().casefold())
        self.assertIn("temporary fallback location", diagnostics.diagnostics_recovery_text())

    def test_all_locations_unavailable_is_graceful(self) -> None:
        (self.root / "standard").write_text("blocker", encoding="utf-8")
        (self.root / "fallback").write_text("blocker", encoding="utf-8")
        path = diagnostics.diagnostics_start()
        self.assertIsNone(path)
        self.assertIsNone(diagnostics.diagnostics_log_path())
        diagnostics.diagnostics_info("No file, no crash")
        self.assertIn("No file, no crash", self.stderr.getvalue())
        self.assertIn("unavailable", self.stderr.getvalue().casefold())
        self.assertIn("No diagnostic log could be created", diagnostics.diagnostics_recovery_text())


class DiagnosticsRotationTests(DiagnosticsTestCase):
    """The file rotates at the module-owned size with the owned backup count."""

    def test_rotation_creates_backup_files(self) -> None:
        with mock.patch.object(diagnostics, "DIAGNOSTICS_MAX_BYTES", 400), mock.patch.object(
            diagnostics, "DIAGNOSTICS_BACKUP_COUNT", 2
        ):
            path = diagnostics.diagnostics_start()
            for index in range(80):
                diagnostics.diagnostics_info("rotation filler", index=index)
        self.assertTrue(path.exists())
        self.assertTrue(Path(str(path) + ".1").exists())

    def test_write_failure_is_nonfatal(self) -> None:
        path = diagnostics.diagnostics_start()
        self.assertTrue(path.exists())
        # Removing the file under the handler must not raise into the caller.
        path.unlink()
        path.parent.rmdir()
        diagnostics.diagnostics_info("after removal")


class DiagnosticsHookTests(DiagnosticsTestCase):
    """Uncaught errors and worker-thread failures reach the log."""

    def test_sys_excepthook_logs_and_is_restored(self) -> None:
        path = diagnostics.diagnostics_start()
        original = sys.excepthook
        diagnostics.diagnostics_install_exception_hooks()
        diagnostics.diagnostics_install_exception_hooks()  # idempotent
        self.assertIsNot(sys.excepthook, original)
        try:
            raise RuntimeError("uncaught boom")
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        diagnostics.diagnostics_shutdown()
        self.assertIs(sys.excepthook, original)
        text = self.logged_text(path)
        self.assertIn("Uncaught exception", text)
        self.assertIn("uncaught boom", text)

    def test_thread_excepthook_logs_worker_failure_and_is_restored(self) -> None:
        path = diagnostics.diagnostics_start()
        original = threading.excepthook
        diagnostics.diagnostics_install_exception_hooks()

        def fail_in_worker() -> None:
            raise ValueError("worker boom")

        worker = threading.Thread(target=fail_in_worker, name="diag-worker")
        worker.start()
        worker.join()
        diagnostics.diagnostics_shutdown()
        self.assertIs(threading.excepthook, original)
        text = self.logged_text(path)
        self.assertIn("Uncaught exception in thread diag-worker", text)
        self.assertIn("worker boom", text)


class DiagnosticsRecoveryTextTests(DiagnosticsTestCase):
    """The advice distinguishes a source checkout from a packaged build."""

    def test_source_advice_names_python_and_tk_minimums_and_log_path(self) -> None:
        diagnostics.diagnostics_start()
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=False), mock.patch.object(
            runtime,
            "runtime_get_requirements",
            return_value={
                "platform": "macos",
                "python": (3, 14, 8),
                "tk": (9, 1, 0),
                "python_text": "3.14.8",
                "tk_text": "9.1.0",
            },
        ):
            text = diagnostics.diagnostics_recovery_text()
            message = runtime.runtime_failure_message("Stage: Runtime\nProblem: old Python")
        self.assertIn("Python to 3.14.8+", text)
        self.assertIn("Tk to 9.1.0+", text)
        self.assertIn(str(self.log_path), text)
        # The installer command belongs to the launcher, not to diagnostics.
        self.assertNotIn("brew", text.casefold())
        self.assertNotIn("python-tk@", text)
        self.assertIn("Recovery advice:", message)
        self.assertIn(str(self.log_path), message)

        # A future policy change must flow through without editing this module.
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=False), mock.patch.object(
            runtime,
            "runtime_get_requirements",
            return_value={
                "platform": "macos",
                "python": (3, 15, 0),
                "tk": (9, 2, 0),
                "python_text": "3.15.0",
                "tk_text": "9.2.0",
            },
        ):
            future = diagnostics.diagnostics_recovery_text()
        self.assertIn("Python to 3.15.0+", future)
        self.assertIn("Tk to 9.2.0+", future)

    def test_frozen_advice_recommends_reinstalling_the_package(self) -> None:
        diagnostics.diagnostics_start()
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=True):
            text = diagnostics.diagnostics_recovery_text()
            message = runtime.runtime_failure_message("Stage: Runtime")
        self.assertIn("packaged application", text)
        self.assertIn("fresh download", text)
        self.assertIn("Recovery advice:", message)

    def test_frozen_report_uses_the_dialog_without_touching_the_desktop(self) -> None:
        diagnostics.diagnostics_start()
        with mock.patch.object(runtime, "runtime_is_frozen", return_value=True), mock.patch.object(
            runtime, "runtime_show_failure_dialog", return_value=True
        ) as dialog:
            runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        self.assertEqual(dialog.call_count, 1)
        self.assertIn(str(self.log_path), dialog.call_args[0][0])
        self.assertIn("Recovery advice:", dialog.call_args[0][0])

    def test_source_failure_with_none_streams_still_reaches_the_log(self) -> None:
        diagnostics.diagnostics_start()
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=False
        ):
            path = runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        self.assertEqual(path, self.log_path)
        self.assertIn("Recovery advice:", self.logged_text(self.log_path))

    def test_frozen_failure_with_none_streams_uses_only_the_dialog(self) -> None:
        diagnostics.diagnostics_start()
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None), mock.patch.object(
            runtime, "runtime_is_frozen", return_value=True
        ), mock.patch.object(runtime, "runtime_show_failure_dialog", return_value=True) as dialog:
            path = runtime.runtime_report_failure("Stage: Runtime\nProblem: broken")
        self.assertEqual(path, self.log_path)
        dialog.assert_called_once()
        self.assertIn("Recovery advice:", dialog.call_args[0][0])


@unittest.skipIf(os.name == "nt", "POSIX viewer command")
class DiagnosticsOpenLogTests(DiagnosticsTestCase):
    """Opening the log uses the platform viewer without launching it in tests."""

    def test_open_log_invokes_the_platform_viewer(self) -> None:
        path = diagnostics.diagnostics_start()
        with mock.patch.object(diagnostics.subprocess, "Popen") as popen:
            self.assertTrue(diagnostics.diagnostics_open_log())
        command = popen.call_args[0][0]
        expected = "/usr/bin/open" if sys.platform.startswith("darwin") else "xdg-open"
        self.assertEqual(command[0], expected)
        self.assertIn(str(path), command)

    def test_open_log_returns_false_without_an_active_log(self) -> None:
        diagnostics.diagnostics_shutdown()
        self.assertFalse(diagnostics.diagnostics_open_log())


class NativeFailureDialogTests(DiagnosticsTestCase):
    """The native failure dialog transports fault text without escaping bugs."""

    @unittest.skipUnless(sys.platform.startswith("darwin"), "osascript dialog")
    def test_macos_dialog_passes_the_full_text_as_argv(self) -> None:
        with mock.patch.object(runtime.subprocess, "Popen") as popen:
            self.assertTrue(runtime.runtime_show_failure_dialog(DIALOG_FAULT_TEXT))
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[1], "-e")
        self.assertEqual(argv[2], runtime.RUNTIME_APPLESCRIPT_DIALOG)
        self.assertEqual(argv[3], DIALOG_FAULT_TEXT)
        self.assertIn("on run argv", argv[2])
        self.assertIn("item 1 of argv", argv[2])

    @unittest.skipUnless(
        sys.platform.startswith("darwin") and shutil.which("osacompile"),
        "osacompile is required",
    )
    def test_macos_dialog_script_compiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "dialog.applescript"
            target = Path(temporary) / "dialog.scpt"
            source.write_text(runtime.RUNTIME_APPLESCRIPT_DIALOG, encoding="utf-8")
            result = subprocess.run(
                ["/usr/bin/osacompile", "-o", str(target), str(source)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.exists())

    def test_windows_dialog_uses_the_blocking_message_box(self) -> None:
        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = mock.MagicMock()
        with mock.patch.object(runtime.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"ctypes": fake_ctypes}
        ), mock.patch.object(runtime.subprocess, "Popen") as popen:
            self.assertTrue(runtime.runtime_show_failure_dialog(DIALOG_FAULT_TEXT))
        fake_ctypes.windll.user32.MessageBoxW.assert_called_once_with(
            0, DIALOG_FAULT_TEXT, "Mediatovideo Converter cannot start", 0x40
        )
        popen.assert_not_called()


class DiagnosticsImportTests(unittest.TestCase):
    """Importing the module must not create handlers or files."""

    def test_import_has_no_side_effects(self) -> None:
        code = (
            "import sys; sys.path.insert(0, {root!r}); "
            "import logging; import mediatovideo_converter.diagnostics as d; "
            "assert logging.getLogger(d.DIAGNOSTICS_LOGGER_NAME).handlers == [], 'handlers installed'; "
            "assert d.diagnostics_log_path() is None, 'log path active'; "
            "print('ok')"
        ).format(root=str(ROOT))
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
