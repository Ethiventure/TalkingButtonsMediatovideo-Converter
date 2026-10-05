"""Runtime compatibility policy and probes for Mediatovideo Converter.

Single owner of the supported Python/Tk versions and of the checks that prove a
candidate can initialize the GUI. Apple's retired Python 3.9.6 / Tk 8.5 can
pass an import-only check, so the probe creates a Tk root with
``ttk.Treeview`` and ``ttk.Progressbar``, lets the event loop draw them, then
destroys them.

Probes run in a supervised child with a hard time limit, and the verdict comes
back through a temporary JSON report file: a windowed frozen app on Windows can
have ``sys.stdout`` set to ``None``. Standard library only, parseable by Python
3.9 so the old system interpreter can probe newer candidates.
"""

from __future__ import annotations

import json
import os
import platform as platform_module
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Policy (single owner). macOS python-tk 3.14 ships Tk 9.1; CPython 3.14.8
# bundles Tcl/Tk 9.0.4 on Windows (PCbuild/tcltk.props).
RUNTIME_MIN_PYTHON: Tuple[int, int, int] = (3, 14, 8)
RUNTIME_MIN_TK_MACOS: Tuple[int, int, int] = (9, 1, 0)
RUNTIME_MIN_TK_WINDOWS: Tuple[int, int, int] = (9, 0, 4)
RUNTIME_MIN_TK_DEFAULT: Tuple[int, int, int] = (8, 6, 0)
RUNTIME_PROBE_TIMEOUT_SECONDS: float = 20.0
RUNTIME_PROBE_REPORT_FLAG: str = "--runtime-probe-report"
_HINT = "Start the app with run_macos.command or run_windows.bat so the launcher can "
# Constant AppleScript: the failure text arrives as argv, so newlines, quotes and
# backslashes in a block can never break out of the dialog source.
RUNTIME_APPLESCRIPT_DIALOG: str = (
    "on run argv\n"
    'display dialog (item 1 of argv) buttons {"OK"} default button "OK" '
    'with title "Mediatovideo Converter cannot start"\n'
    "end run"
)


def runtime_is_frozen() -> bool:
    """Return True inside a PyInstaller-style frozen bundle."""
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def runtime_get_requirements(platform_name: Optional[str] = None) -> Dict[str, object]:
    """Return the supported Python/Tk versions for one platform."""
    value = (platform_name or sys.platform).lower()
    if value.startswith("darwin") or value == "macos":
        key, minimum_tk = "macos", RUNTIME_MIN_TK_MACOS
    elif value.startswith("win"):
        key, minimum_tk = "windows", RUNTIME_MIN_TK_WINDOWS
    else:
        key, minimum_tk = "other", RUNTIME_MIN_TK_DEFAULT
    return {
        "platform": key,
        "python": RUNTIME_MIN_PYTHON,
        "tk": minimum_tk,
        "python_text": ".".join(str(part) for part in RUNTIME_MIN_PYTHON),
        "tk_text": ".".join(str(part) for part in minimum_tk),
    }


def runtime_version_tuple(value: object) -> Tuple[int, ...]:
    """Parse a version into comparable ints ("3.14.8rc1" -> (3, 14, 8))."""
    parts = list(value) if isinstance(value, (tuple, list)) else str(value).strip().split(".")
    parsed: List[int] = []
    for part in parts:
        digits = ""
        for character in str(part):
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parsed.append(int(digits))
    return tuple(parsed)


def runtime_version_supported(found: object, minimum: Sequence[int]) -> bool:
    """Return True when found >= minimum, padding missing parts with zero."""
    if isinstance(found, str) and not re.fullmatch(r"\d+(?:\.\d+)*", found.strip()):
        return False
    found_parts = runtime_version_tuple(found)
    if not found_parts:
        return False
    width = max(len(found_parts), len(minimum))
    padded_found = found_parts + (0,) * (width - len(found_parts))
    padded_minimum = tuple(minimum) + (0,) * (width - len(minimum))
    return padded_found >= padded_minimum


class RuntimeUnsupportedError(RuntimeError):
    """Runtime failure carrying the Stage/Problem/What to do block."""

    def __init__(self, stage: str, problem: str, action: str, details: str = "") -> None:
        self.stage, self.problem, self.action, self.details = stage, problem, action, details
        super().__init__(runtime_error_text(stage, problem, action, details))


def runtime_error_text(stage: str, problem: str, action: str, details: str = "") -> str:
    """Format the canonical visible startup-error block."""
    lines = ["", "=" * 60, " STARTUP ERROR", "=" * 60, "Stage:   " + stage, "Problem: " + problem]
    if details:
        lines.append("Details: " + details)
    lines += ["What to do: " + action, ""]
    return "\n".join(lines)


def runtime_log_failure(text: str) -> Optional[Path]:
    """Write a runtime failure to the diagnostic log and return its path."""
    try:
        from . import diagnostics

        diagnostics.diagnostics_start()
        diagnostics.diagnostics_exception(str(text).rstrip())
        return diagnostics.diagnostics_log_path()
    except Exception:  # noqa: BLE001 - failure reporting must never raise
        return None


def runtime_failure_message(text: str) -> str:
    """Append the actual log path and recovery guidance to a failure block.

    The guidance comes from the diagnostics module, which builds it from
    ``runtime_get_requirements()``: a source checkout is told which Python/Tk
    versions to install, while a frozen application is told to reinstall or
    update the packaged app. The log path is always included so a windowed
    dialog can point at the file even with no terminal.
    """
    advice = ""
    try:
        from . import diagnostics

        diagnostics.diagnostics_start()
        advice = diagnostics.diagnostics_recovery_text()
    except Exception:  # noqa: BLE001 - fall back to a minimal recovery note
        advice = ""
    if not advice:
        requirements = runtime_get_requirements()
        if runtime_is_frozen():
            advice = (
                "Install the latest packaged application version again from a "
                "fresh download and try once more; the diagnostic log could not "
                "be created."
            )
        else:
            advice = (
                "Install or update Python {}+ and Tk {}+, then run the launcher "
                "again; the diagnostic log could not be created."
            ).format(requirements["python_text"], requirements["tk_text"])
    return "{}\nRecovery advice: {}".format(str(text).rstrip(), advice)


def runtime_show_failure_dialog(text: str) -> bool:
    """Show the native windowed failure dialog on macOS/Windows (best effort).

    macOS passes the complete failure block as an osascript argument to a
    constant script, so multi-line text and quoting characters are transported
    verbatim instead of being interpolated into AppleScript source. Windows uses
    the blocking MessageBoxW call as before.
    """
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", RUNTIME_APPLESCRIPT_DIALOG, str(text)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        if sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, "Mediatovideo Converter cannot start", 0x40)
            return True
    except Exception:  # noqa: BLE001 - a dialog is best effort
        return False
    return False


def runtime_report_failure(failure: object) -> Optional[Path]:
    """Report a runtime failure to the terminal, the log and any frozen dialog."""
    text = runtime_failure_message(str(failure))
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    path = runtime_log_failure(text)
    if runtime_is_frozen():
        runtime_show_failure_dialog(text)
    return path


# --- Supervised probe ------------------------------------------------------


def runtime_probe_wrapper_source() -> str:
    """Return the ``-c`` source that runs the probe in a candidate interpreter."""
    root = json.dumps(str(Path(__file__).resolve().parents[1]))
    return (
        "import sys; sys.path.insert(0, {0}); "
        "from mediatovideo_converter.runtime import runtime_probe_main; "
        "raise SystemExit(runtime_probe_main())"
    ).format(root)


def runtime_probe_argv(
    python_executable: Optional[str] = None,
    prefix_args: Sequence[str] = (),
    report_path: Optional[str] = None,
) -> List[str]:
    """Build probe argv for a candidate (frozen bundles re-enter run_app).

    A frozen bundle cannot answer ``-c``, so it starts itself with
    ``--runtime-probe-report``; that child runs no runtime check of its own, so
    there is no recursion.
    """
    if python_executable is None or (
        runtime_is_frozen() and os.path.abspath(python_executable) == os.path.abspath(sys.executable)
    ):
        argv = [sys.executable] if runtime_is_frozen() else [sys.executable, "-c", runtime_probe_wrapper_source()]
    else:
        argv = [str(python_executable), *prefix_args, "-c", runtime_probe_wrapper_source()]
    if report_path:
        argv += [RUNTIME_PROBE_REPORT_FLAG, report_path]
    return argv


def runtime_read_payload(report_path: Optional[str]) -> Optional[Dict[str, object]]:
    """Read the probe verdict from its temporary JSON report file."""
    if not report_path:
        return None
    try:
        payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def runtime_kill_probe(process: "subprocess.Popen[bytes]") -> None:
    """Kill a probe and its children, without relying on PATH on Windows."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot") or r"C:\Windows"
            taskkill = os.path.join(system_root, "System32", "taskkill.exe")
            subprocess.run(
                [taskkill if os.path.exists(taskkill) else "taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def runtime_run_probe(
    argv: Sequence[str],
    timeout_seconds: float = RUNTIME_PROBE_TIMEOUT_SECONDS,
    report_path: Optional[str] = None,
) -> Tuple[Optional[Dict[str, object]], bool, Optional[int], str, Optional[str]]:
    """Run one probe under a watchdog.

    Returns ``(payload, timed_out, returncode, stderr, spawn_error)``. The
    watchdog lives in the parent so a candidate that hangs during import, Tk
    startup or drawing is killed instead of blocking the launcher.
    """
    argv_list = [str(item) for item in argv]
    popen_kwargs: Dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(argv_list, **popen_kwargs)  # type: ignore[arg-type]
    except OSError as error:
        return None, False, None, "", str(error)
    timed_out = False
    try:
        _, stderr_bytes = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        runtime_kill_probe(process)
        try:
            _, stderr_bytes = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stderr_bytes = b""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    return runtime_read_payload(report_path), timed_out, process.returncode, stderr, None


# --- In-process probe (runs inside the candidate interpreter) --------------


def runtime_probe_execute() -> Dict[str, object]:
    """Run the version and widget checks inside the current interpreter.

    Python is checked before Tkinter is imported and the Tk patchlevel before
    any window is created, so an unsupported interpreter (Apple's Tk 8.5) is
    rejected without opening a blank or crashing window.
    """
    requirements = runtime_get_requirements()
    version = platform_module.python_version()
    base: Dict[str, object] = {
        "python_version": version,
        "python_executable": sys.executable,
        "platform": sys.platform,
        "tk_patchlevel": "",
        "tk_version": "",
        "widgets_ok": False,
    }

    def failure(stage: str, problem: str, action: str, details: str = "") -> Dict[str, object]:
        payload: Dict[str, object] = {"ok": False, "stage": stage, "problem": problem,
                                      "action": action, "details": details}
        payload.update(base)
        return payload

    if not runtime_version_supported(sys.version_info[:3], requirements["python"]):
        return failure(
            "Python version",
            "Python {} is older than the supported Python {}.".format(version, requirements["python_text"]),
            _HINT + "install or update Python {}+.".format(requirements["python_text"]),
            "Interpreter: {}".format(sys.executable),
        )
    if sys.version_info.releaselevel != "final":
        return failure("Python version", "Prerelease Python is not supported.", "Install a stable Python release.")
    try:
        import tkinter
        from tkinter import ttk
    except Exception as error:
        return failure(
            "Tkinter",
            "This Python installation does not include Tkinter.",
            "Run the launcher again so it can install the Tk support package, or repair Python.",
            "{}: {}".format(type(error).__name__, error),
        )
    try:
        patchlevel = str(tkinter.Tcl().eval("info patchlevel")).strip()
    except Exception as error:
        return failure(
            "Tk version",
            "The Tcl/Tk library shipped with this Python could not be started.",
            "Reinstall or update Tk for this Python installation, then run the launcher again.",
            "{}: {}".format(type(error).__name__, error),
        )
    base["tk_patchlevel"] = patchlevel
    base["tk_version"] = str(getattr(tkinter, "TkVersion", ""))
    if not runtime_version_supported(patchlevel, requirements["tk"]):
        return failure(
            "Tk version",
            "Tk {} is older than the supported Tk {}.".format(patchlevel, requirements["tk_text"]),
            _HINT + "install or update Tk {}+.".format(requirements["tk_text"]),
            "Interpreter: {}; Tk patchlevel: {}".format(sys.executable, patchlevel),
        )

    root = None
    try:
        root = tkinter.Tk()
        # Tcl's patchlevel may differ from Tk's. Require the actual loaded Tk,
        # not only the interpreter version reported before window creation.
        patchlevel = str(root.tk.call("package", "provide", "Tk"))
        base["tk_patchlevel"] = patchlevel
        if not runtime_version_supported(patchlevel, requirements["tk"]):
            return failure("Tk version", "Tk {} is older than supported Tk {}.".format(patchlevel, requirements["tk_text"]), _HINT + "install a supported Tk release.")
        try:  # Best effort: keep the check window offscreen so nothing flashes.
            root.withdraw()
            root.geometry("+10000+10000")
        except Exception:
            pass
        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("name",), show="headings")
        tree.heading("name", text="Name")
        tree.insert("", "end", values=("probe",))
        tree.pack(fill="both", expand=True)
        bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        bar["value"] = 42
        bar.pack(fill="x")
        try:
            root.deiconify()
        except Exception:
            pass
        root.update_idletasks()
        root.update()
        widgets_ok = bool(
            frame.winfo_exists() and tree.winfo_exists() and bar.winfo_exists() and tree.identify_column(10)
        )
    except Exception as error:
        return failure(
            "GUI widgets",
            "This Python can start Tk but cannot create the window controls the application needs.",
            _HINT + "install or update the Tk drawing kit.",
            "{}: {}".format(type(error).__name__, error),
        )
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    if not widgets_ok:
        return failure(
            "GUI widgets",
            "The Tk window controls did not draw correctly.",
            _HINT + "install or update the Tk drawing kit.",
            "Interpreter: {}; Tk patchlevel: {}".format(sys.executable, patchlevel),
        )
    base["ok"] = True
    base["widgets_ok"] = True
    return base


def runtime_probe_main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the probe and write its JSON to the report file.

    ``--runtime-probe-report PATH`` is the primary transport for windowed frozen
    children; without it the JSON is printed for a terminal that has a stdout.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    report_path: Optional[str] = None
    if RUNTIME_PROBE_REPORT_FLAG in arguments:
        index = arguments.index(RUNTIME_PROBE_REPORT_FLAG)
        if index + 1 < len(arguments):
            report_path = arguments[index + 1]
    payload = runtime_probe_execute()
    text = json.dumps(payload)
    if report_path:
        try:
            Path(report_path).write_text(text, encoding="utf-8")
        except OSError:
            pass
    elif sys.stdout is not None:
        print(text)
    return 0 if payload.get("ok") else 1


# --- Public check API ------------------------------------------------------


def runtime_check(
    python_executable: Optional[str] = None,
    *,
    prefix_args: Sequence[str] = (),
    timeout: Optional[float] = None,
) -> Dict[str, object]:
    """Verify an interpreter can run the GUI; return its probe payload.

    Raises RuntimeUnsupportedError when the candidate is old, broken, missing
    Tk/Tkinter, cannot draw the widgets, or hangs past the time limit.
    """
    limit = RUNTIME_PROBE_TIMEOUT_SECONDS if timeout is None else float(timeout)
    handle = tempfile.NamedTemporaryFile(prefix="mediatovideo-runtime-", suffix=".json", delete=False)
    report_path = handle.name
    handle.close()
    argv = runtime_probe_argv(python_executable, prefix_args, report_path)
    try:
        payload, timed_out, returncode, stderr, spawn_error = runtime_run_probe(argv, limit, report_path)
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass

    if spawn_error:
        raise RuntimeUnsupportedError("Starting Python", "The Python interpreter could not be started.",
                                      _HINT + "install or repair Python.",
                                      "{}: {}".format(argv[0], spawn_error))
    if timed_out:
        raise RuntimeUnsupportedError(
            "Runtime check timed out",
            "The Python interpreter stopped responding while testing the window controls "
            "(limit {} seconds).".format(limit),
            _HINT + "install a working Python and Tk, then try again.",
            "Candidate: {}".format(" ".join(argv)),
        )
    if payload is None:
        raise RuntimeUnsupportedError(
            "Checking Python and Tkinter",
            "The Python interpreter did not report a usable runtime.",
            _HINT + "install or update Python and Tk.",
            "Exit code: {}; output: {}".format(returncode, stderr.strip()[-400:]),
        )
    if not payload.get("ok"):
        raise RuntimeUnsupportedError(
            str(payload.get("stage") or "Checking Python and Tkinter"),
            str(payload.get("problem") or "The runtime check failed."),
            str(payload.get("action") or "Start the app with run_macos.command or run_windows.bat."),
            str(payload.get("details") or ""),
        )
    if returncode != 0 or payload.get("widgets_ok") is not True:
        raise RuntimeUnsupportedError("Checking Python and Tkinter", "The runtime probe did not complete successfully.", _HINT + "repair the runtime.", "Exit code: {}".format(returncode))
    requirements = runtime_get_requirements(str(payload.get("platform") or sys.platform))
    if not runtime_version_supported(str(payload.get("python_version", "")), requirements["python"]) or not runtime_version_supported(str(payload.get("tk_patchlevel", "")), requirements["tk"]):
        raise RuntimeUnsupportedError("Checking Python and Tkinter", "The runtime probe reported unsupported package versions.", _HINT + "install supported stable packages.")
    return payload
