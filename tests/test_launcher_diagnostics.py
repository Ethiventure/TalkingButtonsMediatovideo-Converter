"""Verify launcher diagnostics across normal, failure, and probe entry points."""

from __future__ import annotations

import io
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

import run_app


class LauncherDiagnosticsTests(unittest.TestCase):
    """Check lifecycle behavior without launching Tk or opening native dialogs."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.calls = {}
        for name in ("diagnostics_start", "diagnostics_install_exception_hooks",
                     "diagnostics_info", "diagnostics_exception",
                     "diagnostics_log_path", "diagnostics_recovery_text",
                     "diagnostics_shutdown"):
            self.calls[name] = self.stack.enter_context(mock.patch.object(run_app.diagnostics, name))
        self.calls["diagnostics_log_path"].return_value = Path("/example/application-debug.log")
        self.calls["diagnostics_recovery_text"].return_value = "Use the diagnostic log for recovery."

    def test_logging_starts_before_gui_and_records_exit(self) -> None:
        def launch() -> int:
            self.calls["diagnostics_start"].assert_called_once()
            self.calls["diagnostics_install_exception_hooks"].assert_called_once()
            return 0

        with mock.patch.object(run_app, "run_app_launch_gui", side_effect=launch):
            self.assertEqual(run_app.run_app_main([]), 0)
        self.calls["diagnostics_info"].assert_any_call("Application exit", status=0)
        self.calls["diagnostics_shutdown"].assert_called_once()

    def test_unexpected_startup_exception_gets_logged_and_reported(self) -> None:
        error = OSError("Cannot load GUI resources")
        with mock.patch.object(run_app, "run_app_launch_gui", side_effect=error), \
                mock.patch.object(run_app.runtime, "runtime_report_failure") as report:
            self.assertEqual(run_app.run_app_main([]), 2)
        self.calls["diagnostics_exception"].assert_called_once_with("Unexpected application failure", error)
        self.assertIn("Cannot load GUI resources", report.call_args.args[0])
        self.calls["diagnostics_shutdown"].assert_called_once()

    def test_log_path_does_not_attempt_to_start_tk(self) -> None:
        output = io.StringIO()
        with mock.patch.object(run_app.runtime, "runtime_check") as check, redirect_stdout(output):
            self.assertEqual(run_app.run_app_main(["--log-path"]), 0)
        check.assert_not_called()
        self.assertEqual(output.getvalue().strip(), str(Path("/example/application-debug.log")))

    def test_unavailable_log_path_is_explicit(self) -> None:
        self.calls["diagnostics_log_path"].return_value = None
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_app.run_app_main(["--log-path"]), 1)
        self.assertIn("no writable location", output.getvalue())

    def test_windowed_app_with_none_stdout_can_query_log_path(self) -> None:
        with mock.patch.object(run_app.sys, "stdout", None):
            self.assertEqual(run_app.run_app_main(["--log-path"]), 0)

    def test_internal_runtime_probe_does_not_create_log_session(self) -> None:
        with mock.patch.object(run_app.runtime, "runtime_probe_main", return_value=0):
            self.assertEqual(run_app.run_app_main(["--runtime-probe-report", "/example/probe.json"]), 0)
        self.calls["diagnostics_start"].assert_not_called()
        self.calls["diagnostics_install_exception_hooks"].assert_not_called()

    def test_failure_exit_is_not_reported_as_success(self) -> None:
        with mock.patch.object(run_app, "run_app_launch_gui", return_value=2):
            self.assertEqual(run_app.run_app_main([]), 2)
        self.calls["diagnostics_info"].assert_any_call("Application exit", status=2)


if __name__ == "__main__":
    unittest.main()
