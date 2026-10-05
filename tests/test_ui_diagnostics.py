"""Isolated tests for the GUI's use of the diagnostics contract.

The diagnostics contract is frozen, so the real GUI module is imported
normally. Each test swaps only ``WEB_UI.diagnostics`` for a recording stub and
restores the real module on cleanup, which keeps the stub out of the module
registry and away from the diagnostics and runtime suites. Tk is replaced by
small fakes so the tests run headless; only the behaviour owned by WEB_UI.py is
exercised.
"""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

READY_MESSAGE = "Application window initialized; the application should be running now."
STUB_LOG_PATH = Path("/tmp/stub-diagnostics/diagnostics.log")
STUB_RECOVERY = "Diagnostics: source checkout; logs are written next to the app data."


def _install_diagnostics_stub() -> types.ModuleType:
    """Install the agreed diagnostics contract as a recording stub."""

    stub = types.ModuleType("mediatovideo_converter.diagnostics")
    stub.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    stub.open_result = True
    stub.diagnostics_start = lambda: STUB_LOG_PATH
    stub.diagnostics_log_path = lambda: STUB_LOG_PATH
    stub.diagnostics_recovery_text = lambda: STUB_RECOVERY
    stub.diagnostics_info = lambda message, **fields: stub.calls.append(
        ("info", (message,), fields)
    )
    stub.diagnostics_exception = lambda message, error=None: stub.calls.append(
        ("exception", (message, error), {})
    )
    stub.diagnostics_open_log = lambda: (
        stub.calls.append(("open", (), {})),
        stub.open_result,
    )[1]
    stub.diagnostics_install_exception_hooks = lambda: None
    stub.diagnostics_shutdown = lambda: None
    return stub


STUB = _install_diagnostics_stub()

try:  # The GUI needs Tk; the runtime module reports a Tk-less interpreter.
    import tkinter  # noqa: F401
except Exception as error:  # noqa: BLE001
    raise unittest.SkipTest(f"Tk is required to exercise the GUI integration: {error}")

from mediatovideo_converter import WEB_UI  # noqa: E402


class FakeVariable:
    """Minimal stand-in for a Tk StringVar."""

    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeWidget:
    """Record widget calls and report a drawn width."""

    def __init__(self, width: int = 120, viewable: bool = True) -> None:
        self.width = width
        self.viewable = viewable
        self.states: list[object] = []

    def configure(self, **kwargs: object) -> None:
        self.states.append(kwargs)

    def cget(self, _name: str) -> object:
        return "determinate"

    def winfo_width(self) -> int:
        return self.width

    def winfo_viewable(self) -> bool:
        return self.viewable

    def stop(self) -> None:
        self.states.append("stop")

    def start(self, _interval: int) -> None:
        self.states.append("start")

    def destroy(self) -> None:
        self.states.append("destroy")

    def get_children(self) -> tuple[object, ...]:
        return ()

    def delete(self, *_items: object) -> None:
        return


class FakeTkCall:
    """Stand-in for the Tk interpreter object."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def call(self, *args: object) -> str:
        self.calls.append(args)
        return "9.0.4"


class FakeText(FakeWidget):
    """Stand-in for the activity log Text widget."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def insert(self, _index: object, text: str) -> None:
        self.lines.append(text)

    def delete(self, *_args: object) -> None:
        return

    def see(self, _index: object) -> None:
        return


class FakeRoot(FakeWidget):
    """Stand-in for the Tk root window."""

    def __init__(self, viewable: bool = False) -> None:
        super().__init__(viewable=viewable)
        self.bindings: list[tuple[str, object]] = []
        self.after_idle_calls: list[object] = []
        self.cancelled = False
        self.tk = FakeTkCall()

    def bind(self, sequence: str, callback: object, add: str | None = None) -> None:
        self.bindings.append((sequence, callback))

    def after_idle(self, callback: object) -> None:
        self.after_idle_calls.append(callback)

    def after(self, _delay: int, _callback: object) -> None:
        return

    def destroy(self) -> None:
        self.cancelled = True


def make_app(*, viewable: bool = False) -> WEB_UI.MediaToVideoApp:
    """Build a MediaToVideoApp shell with fakes instead of a real Tk window."""

    app = WEB_UI.MediaToVideoApp.__new__(WEB_UI.MediaToVideoApp)
    app._root = FakeRoot(viewable=viewable)
    app._log = FakeText()
    app._log_line_count = 0
    app._status = FakeVariable()
    app._scan_detail = FakeVariable()
    app._current_detail = FakeVariable()
    app._overall_detail = FakeVariable()
    app._messages = queue.Queue()
    app._cancel_event = threading.Event()
    app._scan_result = None
    app._busy_operation = None
    app._ready_announced = False
    app._diagnostic_log_path = STUB_LOG_PATH
    app._scan_progress = FakeWidget()
    app._current_progress = FakeWidget()
    app._overall_progress = FakeWidget()
    app._scan_button = FakeWidget()
    app._convert_button = FakeWidget()
    app._mkv_button = FakeWidget()
    app._cancel_button = FakeWidget()
    app._groups_table = FakeWidget()
    return app


def make_dialog() -> WEB_UI.MkvToMp4Dialog:
    """Build an MKV dialog shell with fakes instead of a real Toplevel."""

    dialog = WEB_UI.MkvToMp4Dialog.__new__(WEB_UI.MkvToMp4Dialog)
    dialog._window = FakeWidget()
    dialog._source = FakeVariable("/tmp/in/clip.mkv")
    dialog._target = FakeVariable("/tmp/out/clip.mp4")
    dialog._status = FakeVariable()
    dialog._progress_detail = FakeVariable()
    dialog._progress = FakeWidget()
    dialog._open_button = FakeWidget()
    dialog._convert_button = FakeWidget()
    dialog._cancel_button = FakeWidget()
    dialog._messages = queue.Queue()
    dialog._cancel_event = threading.Event()
    dialog._busy = False
    dialog._close_when_done = False
    dialog._completed_output = None
    dialog._ffmpeg_directory = None
    dialog._set_busy = lambda _busy: None  # type: ignore[method-assign]
    return dialog


class UiTestCase(unittest.TestCase):
    """Base case that scopes the stub to one test and mocks every dialog."""

    def setUp(self) -> None:
        STUB.calls.clear()
        STUB.open_result = True
        stub_patcher = mock.patch.object(WEB_UI, "diagnostics", STUB)
        stub_patcher.start()
        self.addCleanup(stub_patcher.stop)
        patcher = mock.patch.object(WEB_UI, "messagebox", mock.MagicMock())
        self.messagebox = patcher.start()
        self.addCleanup(patcher.stop)


class ActivityLogMirrorTests(UiTestCase):
    """The on-screen activity log must also reach the persistent file."""

    def test_activity_log_line_is_mirrored_to_the_diagnostic_file(self) -> None:
        app = make_app()

        app._append_log("Scanning /media/camera")

        self.assertIn(("info", ("Scanning /media/camera",), {}), STUB.calls)
        self.assertEqual(app._log.lines, ["Scanning /media/camera\n"])


class ReadyAnnouncementTests(UiTestCase):
    """Readiness is only announced once the window is really drawn."""

    def test_ready_line_is_not_logged_while_the_window_is_unmapped(self) -> None:
        app = make_app(viewable=False)

        app._announce_ready()

        self.assertFalse(app.ready_announced)
        self.assertNotIn(READY_MESSAGE, [call[1][0] for call in STUB.calls])

    def test_ready_line_logs_path_and_recovery_once_the_window_is_drawn(self) -> None:
        app = make_app(viewable=True)

        app._announce_ready()
        app._announce_ready()

        structured = [call for call in STUB.calls if call[1][0] == READY_MESSAGE]
        self.assertEqual(len(structured), 1)
        self.assertEqual(
            structured[0][2],
            {
                "log": str(STUB_LOG_PATH),
                "tk": "9.0.4",
                "python": sys.version.split()[0],
            },
        )
        self.assertTrue(app.ready_announced)
        self.assertIn("Application should be running now.", app._status.get())
        # The ready record must use the same Tk query as the runtime/selftest.
        self.assertIn(("package", "require", "Tk"), app._root.tk.calls)
        self.assertIn(READY_MESSAGE + "\n", app._log.lines)
        self.assertIn(f"Diagnostic log: {STUB_LOG_PATH}\n", app._log.lines)
        self.assertIn(STUB_RECOVERY + "\n", app._log.lines)


class CallbackExceptionTests(UiTestCase):
    """Tk callback failures must reach the log and a dialog."""

    def test_callback_exception_is_logged_and_shown(self) -> None:
        app = make_app(viewable=True)
        error = RuntimeError("callback exploded")

        app._report_callback_exception(RuntimeError, error, None)

        self.assertIn(("exception", ("Interface callback failed", error), {}), STUB.calls)
        self.messagebox.showerror.assert_called_once()
        self.assertEqual(self.messagebox.showerror.call_args.args[0], "Application error")

class WorkerErrorLoggingTests(UiTestCase):
    """Caught worker errors are logged once, at the catch, with a traceback."""

    def _run_worker_synchronously(self, start: object, *args: object) -> None:
        """Run the worker the method starts, on this thread."""

        captured: dict[str, object] = {}

        class ImmediateThread:
            def __init__(self, target: object, **kwargs: object) -> None:
                captured["target"] = target

            def start(self) -> None:
                return

        module_stub = SimpleNamespace(Thread=ImmediateThread, Event=threading.Event)
        with mock.patch.object(WEB_UI, "threading", module_stub):
            start(*args)  # type: ignore[operator]
        target = captured["target"]
        self.assertIsNotNone(target)
        target()  # type: ignore[operator]

    def test_scan_worker_failure_is_logged_once_at_the_catch(self) -> None:
        app = make_app()
        source = tempfile.mkdtemp(prefix="mediatovideo-ui-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(source, ignore_errors=True))
        app._source = FakeVariable(source)
        app._grouping = FakeVariable(next(iter(WEB_UI._GROUPING_LABELS)))
        app._set_busy = lambda _operation: None  # type: ignore[method-assign]
        error = OSError("drive vanished")

        with mock.patch.object(WEB_UI, "scanner_scan", side_effect=error):
            self._run_worker_synchronously(app._start_scan)

        exceptions = [call for call in STUB.calls if call[0] == "exception"]
        self.assertEqual(exceptions, [("exception", ("Scanning source failed", error), {})])
        self.assertEqual(app._messages.get_nowait()[0], "operation_error")

    def test_conversion_worker_failure_is_logged_once_at_the_catch(self) -> None:
        app = make_app()
        app._output = FakeVariable("/media/out")
        app._layout = FakeVariable(next(iter(WEB_UI._LAYOUT_LABELS)))
        app._format = FakeVariable(next(iter(WEB_UI._FORMAT_LABELS)))
        app._naming = FakeVariable(next(iter(WEB_UI._NAMING_LABELS)))
        app._ffmpeg_directory = FakeVariable("")
        app._scan_result = SimpleNamespace(groups=("group",))
        app._set_busy = lambda _operation: None  # type: ignore[method-assign]
        error = RuntimeError("ffmpeg exploded")

        with mock.patch.object(WEB_UI, "converter_convert", side_effect=error):
            self._run_worker_synchronously(app._start_conversion)

        exceptions = [call for call in STUB.calls if call[0] == "exception"]
        self.assertEqual(exceptions, [("exception", ("Converting videos failed", error), {})])
        self.assertEqual(app._messages.get_nowait()[0], "operation_error")

    def test_queue_delivery_does_not_log_the_error_again(self) -> None:
        app = make_app()
        error = OSError("drive vanished")
        app._messages.put(("operation_error", "Scanning source", error))

        app._poll_messages()

        self.assertEqual([call for call in STUB.calls if call[0] == "exception"], [])

    def test_mkv_worker_failure_is_logged_once_at_the_catch(self) -> None:
        dialog = make_dialog()
        error = RuntimeError("mkv exploded")

        with mock.patch.object(
            WEB_UI, "converter_convert_mkv_to_mp4", side_effect=error
        ):
            self._run_worker_synchronously(dialog._start_conversion)

        exceptions = [call for call in STUB.calls if call[0] == "exception"]
        self.assertEqual(
            exceptions, [("exception", ("MKV to MP4 conversion failed", error), {})]
        )


class MkvLifecycleLoggingTests(UiTestCase):
    """MKV start, success, and cancellation are recorded."""

    def test_mkv_start_success_and_cancellation_are_logged(self) -> None:
        dialog = make_dialog()

        module_stub = SimpleNamespace(Thread=mock.MagicMock(), Event=threading.Event)
        with mock.patch.object(WEB_UI, "threading", module_stub):
            dialog._start_conversion()
        thread = module_stub.Thread
        self.assertIn(
            (
                "info",
                ("MKV to MP4 conversion started",),
                {"source": "/tmp/in/clip.mkv", "target": "/tmp/out/clip.mp4"},
            ),
            STUB.calls,
        )
        self.assertTrue(thread.return_value.start.called)

        dialog._finish_conversion(
            SimpleNamespace(cancelled=False, output=Path("/tmp/out/clip.mp4"))
        )
        self.assertIn(
            (
                "info",
                ("MKV to MP4 conversion completed",),
                {"output": "/tmp/out/clip.mp4"},
            ),
            STUB.calls,
        )

        dialog._busy = True
        dialog._cancel()
        self.assertIn(("info", ("MKV to MP4 cancellation requested",), {}), STUB.calls)
        dialog._finish_conversion(SimpleNamespace(cancelled=True, output=None))
        self.assertIn(("info", ("MKV to MP4 conversion cancelled",), {}), STUB.calls)


class DiagnosticLogControlTests(UiTestCase):
    """The log control never touches a running conversion."""

    def test_opening_the_log_does_not_cancel_running_work(self) -> None:
        app = make_app()
        app._busy_operation = "converting"

        app._open_diagnostic_log()

        self.assertFalse(app._cancel_event.is_set())
        self.assertIn(("open", (), {}), STUB.calls)
        self.assertEqual(app._status.get(), f"Opened diagnostic log: {STUB_LOG_PATH}")

    def test_unavailable_log_reports_recovery_text(self) -> None:
        STUB.open_result = False
        app = make_app()

        app._open_diagnostic_log()

        self.assertIn(STUB_RECOVERY, app._status.get())
        self.assertIn(("info", (f"Diagnostic log unavailable. {STUB_RECOVERY}",), {}), STUB.calls)
        self.messagebox.showwarning.assert_called_once()


class NoGlobalStubLeakTests(unittest.TestCase):
    """The stub must never replace the real diagnostics module globally."""

    def test_real_diagnostics_module_is_still_the_one_the_gui_imports(self) -> None:
        import mediatovideo_converter

        self.assertIs(mediatovideo_converter.diagnostics, WEB_UI.diagnostics)
        self.assertIsNot(WEB_UI.diagnostics, STUB)
        self.assertIs(sys.modules["mediatovideo_converter.diagnostics"], WEB_UI.diagnostics)


if __name__ == "__main__":
    unittest.main()
