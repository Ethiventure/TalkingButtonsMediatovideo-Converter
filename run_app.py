"""Launcher entry point for Mediatovideo Converter.

The runtime preflight happens before the GUI module is imported, so an old or
broken Python/Tk combination is rejected with a Stage/Problem/What to do message
instead of opening a blank window. This file is also the PyInstaller entry
point, which is why a frozen bundle re-enters itself for ``--runtime-probe``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

from mediatovideo_converter import runtime  # noqa: E402
from mediatovideo_converter import diagnostics  # noqa: E402


def run_app_parser() -> argparse.ArgumentParser:
    """Build the launcher argument parser."""
    parser = argparse.ArgumentParser(prog="run_app.py", description="Start Mediatovideo Converter.")
    from mediatovideo_converter import __version__
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check-runtime", action="store_true",
                        help="check this Python/Tk and exit without starting the GUI")
    parser.add_argument("--check-video-tools", action="store_true",
                        help="check FFmpeg/FFprobe through the converter policy and exit")
    parser.add_argument("--runtime-probe", action="store_true",
                        help="internal: run the runtime probe in this process")
    parser.add_argument("--runtime-probe-report", metavar="PATH", default=None,
                        help="internal: write the probe JSON to PATH (frozen transport)")
    parser.add_argument("--self-test", action="store_true", help="run the bundle self test and exit")
    parser.add_argument("--self-test-report", metavar="PATH", default=None,
                        help="write the self-test JSON report to PATH (implies --self-test)")
    parser.add_argument("--log-path", action="store_true",
                        help="print the active diagnostic log path without opening the GUI")
    parser.add_argument("--prepare-recovery", action="store_true",
                        help="verify a packaged app and save its offline repair backup")
    parser.add_argument("--repair-report", metavar="PATH", default=None,
                        help="internal: start offline repair and write its launch result")
    parser.add_argument("--repair-no-relaunch", action="store_true",
                        help="internal: repair without reopening the app (native tests)")
    return parser


def run_app_check_runtime() -> int:
    """Check the current interpreter and print the verdict."""
    try:
        payload = runtime.runtime_check()
    except runtime.RuntimeUnsupportedError as error:
        diagnostics.diagnostics_exception("Runtime compatibility check failed", error)
        print(str(error), file=sys.stderr)
        return 1
    diagnostics.diagnostics_info("Runtime compatibility check passed", **payload)
    print("OK: Compatible Python and Tkinter found\nPython: {} at {}\nTk: {}\n"
          "GUI widgets: Treeview and Progressbar created and drawn".format(
              payload.get("python_version"), payload.get("python_executable"),
              payload.get("tk_patchlevel")))
    return 0


def run_app_video_tools() -> Tuple[int, object]:
    """Check FFmpeg/FFprobe with the policy owned by ``converter.py``."""
    try:
        from mediatovideo_converter.converter import (
            FFmpegCompatibilityError,
            FFmpegNotFoundError,
            converter_find_tools,
            converter_verify_tools,
        )
    except ModuleNotFoundError as error:
        return 3, runtime.runtime_error_text(
            "Checking video tools",
            "The video-tool policy module is not available in this build.",
            "Rebuild the application, or run the test harness from the source checkout.",
            "{}: {}".format(type(error).__name__, error),
        )
    try:
        ffmpeg, ffprobe = converter_find_tools()
        report = converter_verify_tools(ffmpeg, ffprobe)
    except (FFmpegNotFoundError, FFmpegCompatibilityError) as error:
        diagnostics.diagnostics_exception("Video tool compatibility check failed", error)
        # Keep the converter's typed failure for recovery eligibility. Text
        # alone loses the distinction between missing and incompatible tools.
        return 1, error
    tools = report.get("tools", {})
    diagnostics.diagnostics_info(
        "Video tools verified", ffmpeg=ffmpeg, ffprobe=ffprobe,
        ffmpeg_version=tools.get("ffmpeg", {}).get("version", "unknown"),
        ffprobe_version=tools.get("ffprobe", {}).get("version", "unknown"),
        features=report.get("features", {}))
    return 0, ""


def run_app_launch_gui() -> int:
    """Verify the runtime and video tools, then launch the GUI."""
    try:
        payload = runtime.runtime_check()
    except runtime.RuntimeUnsupportedError as error:
        runtime.runtime_report_failure(error)
        return 2
    diagnostics.diagnostics_info("Runtime compatibility check passed", **payload)
    status, failure = run_app_video_tools()
    if status != 0:
        runtime.runtime_report_failure(failure)
        return 2
    run_app_prepare_recovery()
    try:
        from mediatovideo_converter.WEB_UI import web_ui_main
    except ModuleNotFoundError as error:
        if error.name not in {"tkinter", "_tkinter"}:
            raise
        runtime.runtime_report_failure(
            runtime.RuntimeUnsupportedError(
                "Loading the graphical interface",
                "This Python installation does not include Tkinter.",
                "Start the app with run_macos.command or run_windows.bat so the "
                "launcher can install a compatible Python and Tkinter.",
                "{}: {}".format(type(error).__name__, error),
            )
        )
        return 2
    from mediatovideo_converter import __version__
    diagnostics.diagnostics_info("Opening application window", version=__version__)
    web_ui_main()
    return 0


def run_app_prepare_recovery() -> int:
    """Ask the recovery module to preserve a healthy packaged application.

    A failed backup must not prevent normal use. The recovery module owns cache
    location, identity and validation; the launcher only calls its public API.
    """
    if not runtime.runtime_is_frozen():
        return 0
    from mediatovideo_converter.recovery import recovery_seed

    try:
        result = recovery_seed()
    except Exception as error:
        diagnostics.diagnostics_exception("Offline repair backup unavailable", error)
        return 1
    diagnostics.diagnostics_info("Offline repair backup", **result)
    return 0 if result.get("ok") else 1


def run_app_repair(report_path: str, *, relaunch: bool = True) -> int:
    """Transport the recovery module's launch result for native automation.

    This bypasses normal preflight because the target's bundled tools may be
    missing. The recovery module validates the target and replacement itself.
    """
    import json
    from mediatovideo_converter.recovery import recovery_start

    result = recovery_start(relaunch=relaunch)
    destination = Path(report_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 1


def run_app_self_test(report_path: Optional[str]) -> int:
    """Run the bundle self test, owned by ``mediatovideo_converter.self_test``."""
    try:
        from mediatovideo_converter.self_test import self_test_main
    except ModuleNotFoundError as error:
        if error.name not in {"mediatovideo_converter.self_test", "mediatovideo_converter"}:
            raise
        print(runtime.runtime_error_text(
            "Self test",
            "This build does not include the self-test module.",
            "Rebuild the application, or run the test harness from the source checkout.",
            "{}: {}".format(type(error).__name__, error),
        ), file=sys.stderr)
        return 3
    return int(self_test_main(report_path=Path(report_path) if report_path else None))


def run_app_main(argv: Optional[Sequence[str]] = None) -> int:
    """Launch the GUI or run one of the diagnostic entry points."""
    args = run_app_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.runtime_probe:
        return runtime.runtime_probe_main()
    if args.runtime_probe_report:
        return runtime.runtime_probe_main([runtime.RUNTIME_PROBE_REPORT_FLAG, args.runtime_probe_report])
    diagnostics.diagnostics_start()
    diagnostics.diagnostics_install_exception_hooks()
    from mediatovideo_converter import __version__
    diagnostics.diagnostics_info("Application startup", version=__version__)
    diagnostics.diagnostics_info(diagnostics.diagnostics_recovery_text())
    status = 1
    try:
        if args.repair_report:
            status = run_app_repair(args.repair_report, relaunch=not args.repair_no_relaunch)
        elif args.log_path:
            path = diagnostics.diagnostics_log_path()
            if sys.stdout is not None:
                print(str(path) if path else "Diagnostic log unavailable: no writable location.")
            status = 0 if path else 1
        elif args.self_test or args.self_test_report:
            status = run_app_self_test(args.self_test_report)
        elif args.check_runtime:
            status = run_app_check_runtime()
        elif args.prepare_recovery:
            if not runtime.runtime_is_frozen():
                diagnostics.diagnostics_info(
                    "Offline repair preparation requires a packaged application; "
                    "source checkouts use run_macos.command or run_windows.bat.")
                status = 1
            else:
                status = run_app_check_runtime()
                if status == 0:
                    status, failure = run_app_video_tools()
                    if status != 0:
                        runtime.runtime_report_failure(failure)
                    else:
                        status = run_app_prepare_recovery()
        elif args.check_video_tools:
            status, failure = run_app_video_tools()
            if status != 0:
                runtime.runtime_report_failure(failure)
            else:
                diagnostics.diagnostics_info("OK: Compatible FFmpeg and FFprobe found")
        else:
            status = run_app_launch_gui()
    except Exception as error:
        # Import failures and root-construction errors occur before Tk can show
        # its own dialog. The native failure reporter works independently of Tk.
        diagnostics.diagnostics_exception("Unexpected application failure", error)
        runtime.runtime_report_failure(runtime.runtime_error_text(
            "Starting the application", "The application stopped unexpectedly.",
            diagnostics.diagnostics_recovery_text(), f"{type(error).__name__}: {error}"))
        status = 2
    finally:
        diagnostics.diagnostics_info("Application exit", status=status)
        diagnostics.diagnostics_shutdown()
    return status


if __name__ == "__main__":
    raise SystemExit(run_app_main())
