"""Launcher integration with the recovery module's public API."""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import run_app


class RecoveryLauncherTests(unittest.TestCase):
    """Backup errors cannot break an otherwise healthy GUI startup."""

    def test_backup_failure_does_not_prevent_healthy_gui_launch(self) -> None:
        """A disk-full backup remains best effort while the app is usable."""
        recovery = types.ModuleType("mediatovideo_converter.recovery")
        recovery.recovery_seed = mock.Mock(side_effect=OSError("disk full"))
        ui = types.ModuleType("mediatovideo_converter.WEB_UI")
        ui.web_ui_main = mock.Mock()
        with mock.patch.dict("sys.modules", {recovery.__name__: recovery, ui.__name__: ui}), \
                mock.patch.object(run_app.runtime, "runtime_is_frozen", return_value=True), \
                mock.patch.object(run_app.runtime, "runtime_check", return_value={}), \
                mock.patch.object(run_app, "run_app_video_tools", return_value=(0, "")), \
                mock.patch.object(run_app.diagnostics, "diagnostics_info"), \
                mock.patch.object(run_app.diagnostics, "diagnostics_exception") as logged:
            self.assertEqual(run_app.run_app_launch_gui(), 0)
        ui.web_ui_main.assert_called_once_with()
        logged.assert_called_once()

    def test_damaged_app_never_seeds_over_healthy_backup(self) -> None:
        """A video preflight failure must return before preserving the app."""
        with mock.patch.object(run_app.runtime, "runtime_check", return_value={}), \
                mock.patch.object(run_app, "run_app_video_tools", return_value=(1, "missing FFprobe")), \
                mock.patch.object(run_app, "run_app_prepare_recovery") as seed, \
                mock.patch.object(run_app.runtime, "runtime_report_failure"), \
                mock.patch.object(run_app.diagnostics, "diagnostics_info"):
            self.assertEqual(run_app.run_app_launch_gui(), 2)
        seed.assert_not_called()

    def test_repair_report_transports_detached_result_path(self) -> None:
        """Native automation can observe the updater after the failed app exits."""
        recovery = types.ModuleType("mediatovideo_converter.recovery")
        result = {"ok": True, "detail": "staged", "result_path": "repair result.json"}
        recovery.recovery_start = mock.Mock(return_value=result)
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict("sys.modules", {recovery.__name__: recovery}):
            report = Path(temporary) / "reports" / "start.json"
            self.assertEqual(run_app.run_app_repair(str(report), relaunch=False), 0)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), result)
        recovery.recovery_start.assert_called_once_with(relaunch=False)

    def test_tool_preflight_keeps_typed_failure_for_repair_policy(self) -> None:
        """Incompatible bundled tools must retain the converter's error type."""
        from mediatovideo_converter import converter

        error = converter.FFmpegCompatibilityError("incompatible bundled tools")
        with mock.patch.object(converter, "converter_find_tools", side_effect=error), \
                mock.patch.object(run_app.diagnostics, "diagnostics_exception"):
            status, failure = run_app.run_app_video_tools()
        self.assertEqual(status, 1)
        self.assertIs(failure, error)

    def test_source_prepare_reports_that_no_backup_was_created(self) -> None:
        """Explicit preparation cannot falsely succeed in a source checkout."""
        with mock.patch.object(run_app.runtime, "runtime_is_frozen", return_value=False), \
                mock.patch.object(run_app.diagnostics, "diagnostics_start"), \
                mock.patch.object(run_app.diagnostics, "diagnostics_shutdown"), \
                mock.patch.object(run_app.diagnostics, "diagnostics_install_exception_hooks"), \
                mock.patch.object(run_app.diagnostics, "diagnostics_info") as info, \
                mock.patch.object(run_app, "run_app_prepare_recovery") as seed:
            self.assertEqual(run_app.run_app_main(["--prepare-recovery"]), 1)
        seed.assert_not_called()
        self.assertTrue(any("requires a packaged application" in call.args[0]
                            for call in info.call_args_list))


if __name__ == "__main__":
    unittest.main()
