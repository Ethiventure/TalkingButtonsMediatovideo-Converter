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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Policy (single owner). macOS python-tk 3.14 ships Tk 9.1; CPython 3.14.8
# bundles Tcl/Tk 9.0.4 on Windows (PCbuild/tcltk.props).
RUNTIME_MIN_PYTHON: Tuple[int, int, int] = (3, 14, 8)
RUNTIME_MIN_TK_MACOS: Tuple[int, int, int] = (9, 1, 0)
RUNTIME_MIN_TK_WINDOWS: Tuple[int, int, int] = (9, 0, 4)
RUNTIME_MIN_TK_DEFAULT: Tuple[int, int, int] = (8, 6, 0)
RUNTIME_PROBE_TIMEOUT_SECONDS: float = 20.0
#: Bounded wait for the compiled macOS reporter applet to report a choice.
#: The applet auto-dismisses itself after this many seconds, then records
#: "close"; Python waits slightly longer so it always reads that record.
RUNTIME_DIALOG_HELPER_GIVE_UP_SECONDS: int = 300
RUNTIME_DIALOG_HELPER_TIMEOUT_SECONDS: float = 315.0
RUNTIME_PROBE_REPORT_FLAG: str = "--runtime-probe-report"
#: Windows repair dialog labels. The user asked for "Fix automatically", so the
#: native TaskDialog uses these exact labels instead of Yes/No.
RUNTIME_WINDOWS_BUTTON_CLOSE: str = "Close"
RUNTIME_WINDOWS_BUTTON_FIX: str = "Fix automatically"
RUNTIME_WINDOWS_CLOSE_BUTTON_ID: int = 101
RUNTIME_WINDOWS_FIX_BUTTON_ID: int = 102
#: Test hook for the Windows-native CI smoke: when set to a button id, the
#: TaskDialog callback posts TDM_CLICK_BUTTON on TDN_CREATED so the dialog
#: closes itself and the selected id can be asserted without user input.
RUNTIME_WINDOWS_TASK_DIALOG_AUTOCLICK_ID: Optional[int] = None
_HINT = "Start the app with run_macos.command or run_windows.bat so the launcher can "
# Constant AppleScript: the failure text arrives as argv, so newlines, quotes and
# backslashes in a block can never break out of the dialog source.
RUNTIME_APPLESCRIPT_DIALOG: str = (
    "on run argv\n"
    'display dialog (item 1 of argv) buttons {"OK"} default button "OK" '
    'with title "Mediatovideo Converter cannot start"\n'
    "end run"
)
# Constant two-button AppleScript used when a local repair can be offered. The
# failure text still arrives as argv, so no caller text is ever interpolated.
RUNTIME_APPLESCRIPT_REPAIR_DIALOG: str = (
    "on run argv\n"
    "set chosen to button returned of (display dialog (item 1 of argv) "
    'buttons {"Close", "Fix automatically"} default button "Fix automatically" '
    'cancel button "Close" with icon caution '
    'with title "Mediatovideo Converter cannot start")\n'
    "return chosen\n"
    "end run"
)
#: Constant source for the compiled macOS reporter applet. Caller data lives
#: in the private request directory; the script returns repair or close there.
#: Environment variable that names the request directory for the applet.
#: ``open --args`` never reaches an applet's run handler, while ``open --env``
#: does (proven natively), so the request travels through files in that
#: directory: message.txt (dialog text), mode.txt (repair/close) and result.txt.
RUNTIME_DIALOG_HELPER_ENVIRONMENT: str = "MEDIATOVIDEO_DIALOG_DIR"
RUNTIME_APPLESCRIPT_HELPER_SOURCE: str = """on run
set requestDirectory to ""
try
set requestDirectory to (system attribute "__DIALOG_ENV__") as text
end try
set requestValues to readRequest(requestDirectory)
set dialogText to item 1 of requestValues
set actionKind to item 2 of requestValues
set chosen to "close"
if actionKind is "repair" then
try
set dialogResult to (display dialog dialogText buttons {"Close", "Fix automatically"} default button "Fix automatically" cancel button "Close" with icon caution with title "Mediatovideo Converter cannot start" giving up after __GIVE_UP__)
set chosen to repairChoice(dialogResult)
on error
set chosen to "close"
end try
else
try
display dialog dialogText buttons {"OK"} default button "OK" with icon caution with title "Mediatovideo Converter cannot start" giving up after __GIVE_UP__
set chosen to "close"
on error
set chosen to "close"
end try
end if
writeResult(chosen, requestDirectory & "/result.txt")
return chosen
end run

on readRequest(requestDirectory)
set dialogText to ""
set actionKind to "close"
if requestDirectory is not "" then
try
set dialogText to (read (POSIX file (requestDirectory & "/message.txt")) as \u00abclass utf8\u00bb)
set actionKind to (read (POSIX file (requestDirectory & "/mode.txt")) as \u00abclass utf8\u00bb)
if actionKind is not "repair" then set actionKind to "close"
on error
set actionKind to "close"
end try
end if
return {dialogText, actionKind}
end readRequest

on repairChoice(dialogResult)
set decision to "close"
try
if not (gave up of dialogResult) then
if (button returned of dialogResult) is "Fix automatically" then
set decision to "repair"
end if
end if
on error
set decision to "close"
end try
return decision
end repairChoice

on writeResult(chosen, resultPath)
set resultFileExists to false
try
get info for (POSIX file resultPath)
set resultFileExists to true
on error
set resultFileExists to false
end try
if resultFileExists then
try
set fileHandle to open for access POSIX file resultPath with write permission
set eof of fileHandle to 0
write chosen to fileHandle
close access fileHandle
on error
try
close access POSIX file resultPath
end try
end try
end if
end writeResult""".replace("__GIVE_UP__", str(RUNTIME_DIALOG_HELPER_GIVE_UP_SECONDS)).replace(
    "__DIALOG_ENV__", RUNTIME_DIALOG_HELPER_ENVIRONMENT
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


def runtime_log_info(message: str, **fields: object) -> None:
    """Record an informational entry in the diagnostic log, never raising."""
    try:
        from . import diagnostics

        diagnostics.diagnostics_info(message, **fields)
    except Exception:  # noqa: BLE001 - logging must never break the reporter
        pass


def runtime_log_exception(message: str, error: object) -> None:
    """Record an error and traceback in the diagnostic log, never raising."""
    try:
        from . import diagnostics

        diagnostics.diagnostics_exception(message, error)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - logging must never break the reporter
        pass


def runtime_frozen_app_path() -> Optional[Path]:
    """Return the frozen application that is running right now.

    Returns ``None`` outside a frozen bundle, because a source interpreter can
    live inside an unrelated ``.app`` (for example an IDE). In a frozen build a
    macOS bundle reports the executable inside ``Something.app/Contents/MacOS``
    so the owning ``.app`` is returned; Windows returns the executable path.
    Repairing must always target this running copy (for example a disposable
    damaged test copy) rather than any canonical installation.
    """
    if not runtime_is_frozen():
        return None
    try:
        executable = Path(sys.executable).resolve()
    except Exception:  # noqa: BLE001 - the path is only used for display
        return None
    if sys.platform.startswith("darwin"):
        for candidate in (executable, *executable.parents):
            if candidate.suffix == ".app":
                return candidate
    return executable


def runtime_repair_target_text(recovery_module: object = None) -> str:
    """Return the application path shown in the repair popup.

    The recovery module owns the current-copy policy and is asked through
    ``recovery_target_text()``. Its ``""`` answer means "no packaged target" and
    is respected as a neutral label. The locally derived frozen path is only a
    fallback for builds whose recovery module does not expose the getter.
    """
    getter = getattr(recovery_module, "recovery_target_text", None)
    if callable(getter):
        try:
            target = str(getter()).strip()
            return target or "this application copy"
        except Exception:  # noqa: BLE001 - fall back to the local path
            pass
    app_path = runtime_frozen_app_path()
    return str(app_path) if app_path is not None else "this application copy"


def runtime_repair_offer_text(text: str, target_text: str) -> str:
    """Return the dialog text shown when a local repair can be offered."""
    return (
        "{}\nTarget: {}\nFix automatically restores this copy from the verified "
        "local application cache. Close leaves it unchanged."
    ).format(str(text).rstrip(), target_text)


def runtime_show_repair_dialog(text: str) -> Optional[bool]:
    """Ask whether to repair automatically.

    Returns True for "Fix automatically", False for Close/cancel/"No", and None
    when the native dialog cannot be shown at all. macOS blocks on a constant
    two-button AppleScript whose only variable is the argv text; Windows prefers
    a TaskDialog with Close/Fix automatically, with a Yes/No MessageBox fallback.
    """
    try:
        if sys.platform.startswith("darwin"):
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", RUNTIME_APPLESCRIPT_REPAIR_DIALOG, str(text)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            chosen = (result.stdout or "").strip()
            return result.returncode == 0 and chosen == "Fix automatically"
        if sys.platform.startswith("win"):
            choice = runtime_windows_task_dialog(text)
            if choice is not None:
                return choice
            return runtime_windows_message_box(text)
    except Exception:  # noqa: BLE001 - a missing dialog is reported as None
        return None
    return None


def runtime_windows_dialog_definition(text: str) -> Dict[str, object]:
    """Return the exact TaskDialog content used on Windows.

    Plain data so the button labels, default button and wording can be checked
    on any platform without opening a dialog.
    """
    content = (
        "{}\n\nChoose {} to restore this application from the verified local "
        "cache, or {} to leave it unchanged."
    ).format(str(text).rstrip(), RUNTIME_WINDOWS_BUTTON_FIX, RUNTIME_WINDOWS_BUTTON_CLOSE)
    return {
        "title": "Mediatovideo Converter cannot start",
        "main_instruction": "A startup problem was found",
        "content": content,
        "buttons": [
            {
                "id": RUNTIME_WINDOWS_CLOSE_BUTTON_ID,
                "label": RUNTIME_WINDOWS_BUTTON_CLOSE,
                "result": False,
            },
            {
                "id": RUNTIME_WINDOWS_FIX_BUTTON_ID,
                "label": RUNTIME_WINDOWS_BUTTON_FIX,
                "result": True,
            },
        ],
        "default_button_id": RUNTIME_WINDOWS_FIX_BUTTON_ID,
    }


def runtime_windows_task_dialog_structures() -> Optional[Tuple[object, object]]:
    """Return the TASKDIALOG_BUTTON/TASKDIALOGCONFIG ctypes types, or None.

    Field order follows commctrl.h with default alignment: 16-byte button and
    176-byte config on 64-bit, 96-byte config on 32-bit. Tests assert the
    measured offsets so a packing mistake cannot ship silently.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - unavailable ctypes means no TaskDialog
        return None

    class _TaskDialogButton(ctypes.Structure):
        _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", wintypes.LPCWSTR)]

    class _TaskDialogConfig(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwndParent", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("dwFlags", wintypes.UINT),
            ("dwCommonButtons", wintypes.UINT),
            ("pszWindowTitle", wintypes.LPCWSTR),
            ("pszMainIcon", wintypes.LPCWSTR),
            ("pszMainInstruction", wintypes.LPCWSTR),
            ("pszContent", wintypes.LPCWSTR),
            ("cButtons", wintypes.UINT),
            ("pButtons", ctypes.POINTER(_TaskDialogButton)),
            ("nDefaultButton", ctypes.c_int),
            ("cRadioButtons", wintypes.UINT),
            ("pRadioButtons", ctypes.POINTER(_TaskDialogButton)),
            ("nDefaultRadioButton", ctypes.c_int),
            ("pszVerificationText", wintypes.LPCWSTR),
            ("pszExpandedInformation", wintypes.LPCWSTR),
            ("pszExpandedControlText", wintypes.LPCWSTR),
            ("pszCollapsedControlText", wintypes.LPCWSTR),
            ("pszFooterIcon", wintypes.LPCWSTR),
            ("pszFooter", wintypes.LPCWSTR),
            ("pfCallback", ctypes.c_void_p),
            ("lpCallbackData", ctypes.c_ssize_t),
            ("cxWidth", wintypes.UINT),
        ]

    return _TaskDialogButton, _TaskDialogConfig


def runtime_windows_post_dialog_click(hwnd: object, button_id: int) -> bool:
    """Post TDM_CLICK_BUTTON to a TaskDialog with explicit 64-bit-safe types.

    The argtypes/restype are set every call because an untyped ctypes call would
    pass HWND/WPARAM/LPARAM as C ints and truncate them on 64-bit Windows.
    """
    try:
        import ctypes
        from ctypes import wintypes

        function = ctypes.windll.user32.PostMessageW
        function.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        function.restype = wintypes.BOOL
        return bool(function(hwnd, 0x0466, button_id, 0))  # TDM_CLICK_BUTTON
    except Exception:  # noqa: BLE001 - a failed post is a failed post
        return False


def runtime_windows_task_dialog(text: str) -> Optional[bool]:
    """Show the native Windows TaskDialog with exact Close/Fix automatically labels.

    Returns True for "Fix automatically", False for Close/cancel, and None when
    TaskDialogIndirect or comctl32 is unavailable so the caller can fall back to
    the plain Yes/No MessageBox. When
    ``RUNTIME_WINDOWS_TASK_DIALOG_AUTOCLICK_ID`` is set, a real TDN_CREATED
    callback posts TDM_CLICK_BUTTON so CI can exercise the native ABI without
    leaving an interactive dialog behind.
    """
    try:
        import ctypes
        from ctypes import wintypes

        structures = runtime_windows_task_dialog_structures()
        if structures is None:
            return None
        _TaskDialogButton, _TaskDialogConfig = structures
        definition = runtime_windows_dialog_definition(text)

        entries = list(definition["buttons"])  # type: ignore[arg-type]
        buttons = (_TaskDialogButton * len(entries))(
            *[_TaskDialogButton(int(entry["id"]), str(entry["label"])) for entry in entries]
        )
        config = _TaskDialogConfig()
        config.cbSize = ctypes.sizeof(_TaskDialogConfig)
        config.pszWindowTitle = str(definition["title"])
        config.pszMainInstruction = str(definition["main_instruction"])
        config.pszContent = str(definition["content"])
        config.cButtons = len(entries)
        config.pButtons = buttons
        config.nDefaultButton = int(definition["default_button_id"])
        config.dwFlags = 0x0008  # TDF_ALLOW_DIALOG_CANCELLATION

        callback_holder = None
        if RUNTIME_WINDOWS_TASK_DIALOG_AUTOCLICK_ID is not None:
            callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                ctypes.c_ssize_t,
            )
            click_id = int(RUNTIME_WINDOWS_TASK_DIALOG_AUTOCLICK_ID)

            def _auto_click_callback(hwnd, message, wparam, lparam, data):
                if message == 0:  # TDN_CREATED
                    runtime_windows_post_dialog_click(hwnd, click_id)
                return 0

            callback_holder = callback_type(_auto_click_callback)
            config.pfCallback = ctypes.cast(callback_holder, ctypes.c_void_p).value

        library = ctypes.windll.comctl32
        function = library.TaskDialogIndirect
        function.argtypes = [
            ctypes.POINTER(_TaskDialogConfig),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        function.restype = ctypes.c_long
        selected = ctypes.c_int(0)
        hresult = function(ctypes.byref(config), ctypes.byref(selected), None, None)
        del callback_holder
        if hresult != 0:
            return None
        for entry in entries:
            if int(entry["id"]) == selected.value:
                return bool(entry["result"])
        return False
    except Exception:  # noqa: BLE001 - unavailable TaskDialog falls back to MessageBox
        return None


def runtime_windows_message_box(text: str) -> Optional[bool]:
    """Fallback Windows Yes/No dialog where Yes means Fix automatically."""
    try:
        import ctypes

        message = (
            "{}\n\nChoose Yes to fix automatically using the verified local "
            "application cache, or No to close without changing anything."
        ).format(str(text).rstrip())
        answer = ctypes.windll.user32.MessageBoxW(
            0, message, "Mediatovideo Converter cannot start", 0x04 | 0x30
        )
        return answer == 6
    except Exception:  # noqa: BLE001 - no dialog is reported as None
        return None


def runtime_dialog_helper_name() -> str:
    """Return the compiled macOS reporter applet bundle name."""
    return "Mediatovideo Startup Reporter.app"


def runtime_dialog_helper_source() -> str:
    """Return the constant AppleScript source for the macOS reporter applet.

    The build script compiles this with ``/usr/bin/osacompile`` before signing.
    Caller data is read from fixed files in a private request directory, so no
    failure text is ever interpolated into AppleScript source.
    """
    return RUNTIME_APPLESCRIPT_HELPER_SOURCE


def runtime_dialog_helper_path() -> Optional[Path]:
    """Return the compiled reporter applet inside the running frozen bundle.

    Returns ``None`` when the platform is not macOS, the process is not frozen,
    or the applet is not present; callers then use the legacy osascript dialog.
    """
    if not runtime_is_frozen() or not sys.platform.startswith("darwin"):
        return None
    candidates: List[Path] = []
    app_path = runtime_frozen_app_path()
    if app_path is not None and app_path.suffix == ".app":
        candidates.append(app_path / "Contents" / "Frameworks" / runtime_dialog_helper_name())
    implementation_root = getattr(sys, "_MEIPASS", None)
    if implementation_root:
        candidates.append(Path(str(implementation_root)) / runtime_dialog_helper_name())
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def runtime_launch_dialog_helper(
    text: str,
    allow_repair: bool,
    timeout: Optional[float] = None,
) -> Tuple[bool, Optional[bool]]:
    """Launch the reporter applet and wait for its bounded choice.

    Returns ``(launched, choice)``. ``launched`` is False only when the applet
    could not be started at all, so the caller may fall back to the legacy
    dialog. Once launched, ``choice`` is True for "repair", False for "close",
    and None when the user did not answer within the bound; a timeout must not
    trigger a second dialog. The temporary result file is removed on every
    path, and the applet refuses to recreate it once it is gone.

    LaunchServices does not deliver ``open --args`` to an applet, so the request
    travels through a unique directory named by
    ``MEDIATOVIDEO_DIALOG_DIR``: message.txt, mode.txt and result.txt.
    """
    helper = runtime_dialog_helper_path()
    if helper is None:
        return False, None
    limit = RUNTIME_DIALOG_HELPER_TIMEOUT_SECONDS if timeout is None else float(timeout)
    request_directory = Path(tempfile.mkdtemp(prefix="mediatovideo-dialog-"))
    result_path = request_directory / "result.txt"
    try:
        (request_directory / "message.txt").write_text(str(text), encoding="utf-8")
        (request_directory / "mode.txt").write_text(
            "repair" if allow_repair else "close", encoding="utf-8"
        )
        result_path.write_text("", encoding="utf-8")
    except OSError:
        shutil.rmtree(request_directory, ignore_errors=True)
        return False, None
    try:
        process = subprocess.Popen(
            [
                "/usr/bin/open",
                "-n",
                "--env",
                "{}={}".format(RUNTIME_DIALOG_HELPER_ENVIRONMENT, request_directory),
                str(helper),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - a failed launch falls back to osascript
        shutil.rmtree(request_directory, ignore_errors=True)
        return False, None
    runtime_log_info("Dialog applet launched", channel="macos-reporter", helper=str(helper))

    deadline = time.monotonic() + limit
    try:
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code not in (None, 0):
                # `open` exited nonzero: LaunchServices rejected the applet, so
                # the caller must fall back to the legacy dialog immediately.
                runtime_log_info("Dialog applet rejected", channel="macos-reporter", exit_code=exit_code)
                return False, None
            try:
                chosen = result_path.read_text(encoding="utf-8").strip()
            except OSError:
                chosen = ""
            if chosen in {"repair", "close"}:
                return True, chosen == "repair"
            time.sleep(0.1)
        return True, None
    finally:
        shutil.rmtree(request_directory, ignore_errors=True)


def runtime_dialog_choice(
    text: str,
    *,
    allow_repair: bool,
    timeout: Optional[float] = None,
) -> Tuple[bool, Optional[bool]]:
    """Return ``(dialog_started, choice)``, preferring the reporter applet.

    ``choice`` is True for repair/yes, False for close/no, and None when the
    dialog started but no answer arrived within the bound. ``dialog_started`` is
    False when neither the applet nor the legacy dialog could be shown. An
    applet that started and timed out returns ``(True, None)`` so callers never
    open a duplicate dialog; an applet that failed to launch falls back to the
    legacy osascript/MessageBoxW path (repair dialog when allowed, otherwise the
    original fire-and-forget notice).
    """
    if runtime_dialog_helper_path() is not None:
        launched, choice = runtime_launch_dialog_helper(text, allow_repair, timeout=timeout)
        if launched:
            return True, choice
    legacy_channel = (
        "macos-osascript"
        if sys.platform.startswith("darwin")
        else "windows-messagebox"
        if sys.platform.startswith("win")
        else "unsupported"
    )
    if allow_repair:
        choice = runtime_show_repair_dialog(text)
        runtime_log_info("Legacy repair dialog", channel=legacy_channel, started=choice is not None)
        if choice is None:
            return False, None
        return True, choice
    started = runtime_show_failure_dialog(text)
    runtime_log_info("Legacy notice dialog", channel=legacy_channel, started=bool(started))
    if not started:
        return False, None
    return True, False


def runtime_show_repair_failure(detail: str) -> bool:
    """Show the no-repair failure notice, including the diagnostic log path.

    Reuses the fire-and-forget single-button dialog and never offers another
    repair, so a failed repair cannot recurse.
    """
    log_note = "The diagnostic log could not be created."
    try:
        from . import diagnostics

        path = diagnostics.diagnostics_log_path()
        if path is not None:
            log_note = "Diagnostic log: {}".format(path)
    except Exception:  # noqa: BLE001 - the notice is best effort
        pass
    text = "{}\nThe automatic repair did not complete and no changes were made.\n{}".format(
        str(detail).rstrip() or "Unknown repair failure.", log_note
    )
    _started, choice = runtime_dialog_choice(text, allow_repair=False)
    return bool(choice)


def runtime_no_repair_notice(text: str) -> str:
    """Append the explicit no-repair explanation for a frozen no-offer popup.

    Only frozen builds use this text, so source-mode guidance is unchanged. It
    explains why the Fix button is absent without importing or changing the
    recovery module's eligibility policy.
    """
    return (
        "{}\nAutomatic repair is not available for this problem. Install the "
        "complete application again from a fresh download."
    ).format(str(text).rstrip())


def runtime_repair_outcome(failure: object) -> str:
    """Offer and run the local repair; return one of six outcome strings.

    ``not-offered`` means the recovery module declined (or is unavailable),
    ``unavailable`` means no native dialog could be shown at all, ``no-answer``
    means the dialog started but the user did not answer within the bound,
    ``declined`` means the user chose Close/No or the applet gave up, and
    ``repair-started`` means recovery_start() staged the repair and started its
    detached helper; final completion is logged by the recovery worker itself.
    ``failed`` means recovery_start() reported (or raised) a failure. Callers
    must not open another dialog for ``no-answer``, ``declined``,
    ``repair-started`` or ``failed``.

    The recovery module is imported lazily and owns the verified local cache
    policy, so this reporter stays independent of the repair worker and tests
    can inject a fake into ``sys.modules``. ``failure`` is handed to
    ``recovery_can_offer`` exactly as received (a converter exception object or
    a failure string); only the dialog text is formatted from it.
    """
    try:
        from . import recovery
    except Exception:  # noqa: BLE001 - no module means no offer
        return "not-offered"
    can_offer = getattr(recovery, "recovery_can_offer", None)
    start_repair = getattr(recovery, "recovery_start", None)
    if not callable(can_offer) or not callable(start_repair):
        return "not-offered"
    try:
        offered = bool(can_offer(failure))
    except Exception as error:  # noqa: BLE001 - a broken policy is a refusal
        runtime_log_exception("Repair availability check failed", error)
        return "not-offered"
    if not offered:
        return "not-offered"

    failure_text = str(failure)
    target = runtime_repair_target_text(recovery)
    runtime_log_info("Repair requested", target=target)
    started, choice = runtime_dialog_choice(
        runtime_repair_offer_text(runtime_failure_message(failure_text), target),
        allow_repair=True,
    )
    if not started:
        runtime_log_info("Repair dialog unavailable", target=target)
        return "unavailable"
    if choice is None:
        runtime_log_info("Repair dialog ended without a choice", target=target)
        return "no-answer"
    if not choice:
        runtime_log_info("Repair declined", target=target)
        return "declined"

    runtime_log_info("Repair preparing", target=target)
    try:
        result = start_repair(relaunch=True)
    except Exception as error:  # noqa: BLE001 - a raised repair is a failed repair
        runtime_log_exception("Repair failed", error)
        runtime_show_repair_failure("{}: {}".format(type(error).__name__, error))
        return "failed"

    if isinstance(result, dict):
        ok = bool(result.get("ok"))
        detail = str(result.get("detail") or "")
        result_path = result.get("result_path")
    else:
        ok = bool(result)
        detail = ""
        result_path = None
    runtime_log_info("Repair launch result", ok=ok, detail=detail or None,
                     result_path=result_path, target=target)
    if not ok:
        runtime_show_repair_failure(detail or "The recovery worker reported a failure.")
        return "failed"
    return "repair-started"


def runtime_report_failure(failure: object) -> Optional[Path]:
    """Report a runtime failure to the terminal, the log and any frozen dialog.

    A frozen build first asks the recovery module whether the local cache can
    repair this failure. When it can, the native popup offers "Fix
    automatically" and the blocking selection drives recovery_start(). When it
    cannot, the no-repair popup is shown: the compiled helper applet when it is
    bundled, otherwise the original fire-and-forget osascript/MessageBoxW path.
    """
    text = runtime_failure_message(str(failure))
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    path = runtime_log_failure(text)
    if runtime_is_frozen():
        try:
            outcome = runtime_repair_outcome(failure)
        except Exception:  # noqa: BLE001 - never block the original report
            outcome = "not-offered"
        if outcome in {"not-offered", "unavailable"}:
            runtime_dialog_choice(runtime_no_repair_notice(text), allow_repair=False)
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
