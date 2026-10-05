"""Persistent, rotating application diagnostics log for Mediatovideo Converter.

This module owns the log location policy, the retention limits and the thread
safety, and it keeps every failure nonfatal: if no directory is writable the
application still starts, the reason is mirrored to an existing stderr, and
``diagnostics_log_path()`` returns ``None`` so callers can tell the user where
the log should have been. The single log file is UTF-8 and rotates at 2 MiB
with three backups.

Importing this module never creates a file, installs a handler or touches the
streams; the session begins with ``diagnostics_start()``. A windowed frozen app
may have ``sys.stdout``/``sys.stderr`` equal to ``None``, so every stream use is
guarded and the persistent file is written independently of the streams.

Standard library only, and parseable by Python 3.9 so the old system interpreter
can record early runtime failures before a supported Python exists.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

# Module-owned location and retention policy.
DIAGNOSTICS_LOG_BASENAME = "application-debug.log"
DIAGNOSTICS_MAX_BYTES = 2 * 1024 * 1024
DIAGNOSTICS_BACKUP_COUNT = 3
DIAGNOSTICS_LOGGER_NAME = "mediatovideo.diagnostics"
_APPLICATION_DIRECTORY = "Mediatovideo Converter"
_UNIX_DIRECTORY = "mediatovideo-converter"
_MAX_FIELD_CHARS = 300
_MAX_MESSAGE_CHARS = 2000
_MAX_RECORD_CHARS = 1000
_MAX_ITEMS = 8
_MAX_TRACEBACK_CHARS = 8000
_LOG_FORMAT = "%(asctime)s %(levelname)s [pid=%(process)d session=%(session)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"


def _diagnostics_standard_log_path() -> Path:
    """Return the documented per-user log path for this platform."""
    if sys.platform.startswith("darwin"):
        return Path.home() / "Library" / "Logs" / _APPLICATION_DIRECTORY / DIAGNOSTICS_LOG_BASENAME
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
        return base / _APPLICATION_DIRECTORY / "Logs" / DIAGNOSTICS_LOG_BASENAME
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / _UNIX_DIRECTORY / DIAGNOSTICS_LOG_BASENAME


def _diagnostics_fallback_log_path() -> Path:
    """Return the temporary-directory fallback used when the standard path fails."""
    return Path(tempfile.gettempdir()) / _APPLICATION_DIRECTORY / DIAGNOSTICS_LOG_BASENAME


def _diagnostics_clean(text: object) -> str:
    """Return one log-safe line: no control characters and bounded length."""
    value = str(text).replace("\r", " ").replace("\n", "\\n").replace("\t", " ")
    if len(value) > _MAX_FIELD_CHARS:
        value = value[:_MAX_FIELD_CHARS] + "..."
    return value


def _diagnostics_render(value: object, depth: int = 0) -> str:
    """Render a field value without dumping unbounded structures or secrets."""
    try:
        if value is None or isinstance(value, (bool, int, float, Path)):
            return _diagnostics_clean(value)
        if isinstance(value, str):
            return _diagnostics_clean(value)
        if depth >= 1:
            return "<{}>".format(type(value).__name__)
        if isinstance(value, dict):
            entries = list(value.items())[:_MAX_ITEMS]
            body = ", ".join(
                "{}: {}".format(_diagnostics_clean(key), _diagnostics_render(item, depth + 1))
                for key, item in entries
            )
            if len(value) > len(entries):
                body += ", ..."
            return "{" + body + "}"
        if isinstance(value, (list, tuple, set, frozenset)):
            entries = list(value)[:_MAX_ITEMS]
            body = ", ".join(_diagnostics_render(item, depth + 1) for item in entries)
            if len(value) > len(entries):
                body += ", ..."
            return "[" + body + "]"
        return _diagnostics_clean(repr(value))
    except Exception:  # noqa: BLE001 - rendering must never raise
        return "<unrenderable>"


def _diagnostics_compose(message: object, fields: Dict[str, Any]) -> str:
    """Join a message and its fields into one deterministic log line."""
    text = _diagnostics_bounded_message(message)
    if not fields:
        return text
    rendered = " ".join(
        "{}={}".format(key, _diagnostics_render(fields[key])) for key in sorted(fields)
    )
    if not rendered:
        return text
    composed = "{} {}".format(text, rendered)
    if len(composed) > _MAX_RECORD_CHARS:
        composed = composed[:_MAX_RECORD_CHARS] + "..."
    return composed


def _diagnostics_bounded_message(message: object) -> str:
    """Bound a caller-supplied message without destroying its structure.

    Messages may legitimately span several lines (a formatted failure block or a
    traceback), so newlines are preserved and only an extreme length is trimmed.
    """
    text = str(message).rstrip()
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[:_MAX_MESSAGE_CHARS] + "..."
    return text


class _DiagnosticsFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating UTF-8 handler whose write and rotation errors never propagate."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        _diagnostics_note_write_failure()


class _DiagnosticsStreamHandler(logging.StreamHandler):
    """stderr mirror that tolerates a stream that is None or already closed."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self, "stream", None) is None:
            return
        try:
            super().emit(record)
        except Exception:  # noqa: BLE001 - terminal mirroring is best effort
            pass


class _DiagnosticsFilter(logging.Filter):
    """Inject the session id so every record carries it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session = _state.session_id or "----------"
        return True


class _DiagnosticsState:
    """Mutable session state guarded by ``lock``."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started = False
        self.logger: Optional[logging.Logger] = None
        self.file_handler: Optional[logging.Handler] = None
        self.stream_handler: Optional[logging.Handler] = None
        self.path: Optional[Path] = None
        self.fallback = False
        self.last_error = ""
        self.session_id = ""
        self.write_failure_noted = False
        self.hooks_installed = False
        self.previous_sys_hook: Any = None
        self.previous_thread_hook: Any = None


_state = _DiagnosticsState()


def _diagnostics_application_version() -> str:
    """Return the application version without failing when it is unavailable."""
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - diagnostics must never fail the caller
        return "unknown"


def _diagnostics_runtime_requirements() -> Optional[Dict[str, object]]:
    """Return runtime requirements through the runtime module's public getter."""
    try:
        from . import runtime

        return runtime.runtime_get_requirements()
    except Exception:  # noqa: BLE001 - diagnostics must never fail the caller
        return None


def _diagnostics_is_frozen() -> bool:
    """Return the frozen flag through the runtime module's public getter."""
    try:
        from . import runtime

        return bool(runtime.runtime_is_frozen())
    except Exception:  # noqa: BLE001 - diagnostics must never fail the caller
        return bool(getattr(sys, "frozen", False))


def _diagnostics_write_stderr(text: str) -> None:
    """Write a fallback line to stderr when one exists, never raising."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(text.rstrip() + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001 - a broken stream must not be fatal
        pass


def _diagnostics_note_write_failure() -> None:
    """Report a handler write/rotation failure once, then stay quiet."""
    if _state.write_failure_noted:
        return
    _state.write_failure_noted = True
    _diagnostics_write_stderr(
        "Diagnostics log write failed; the application continues without it."
    )


def _diagnostics_build_file_handler(path: Path) -> _DiagnosticsFileHandler:
    """Create the rotating handler, raising OSError when the path is unusable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _DiagnosticsFileHandler(
        str(path),
        maxBytes=DIAGNOSTICS_MAX_BYTES,
        backupCount=DIAGNOSTICS_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    handler.addFilter(_DiagnosticsFilter())
    return handler


def _diagnostics_session_fields() -> Dict[str, Any]:
    """Return the session metadata recorded once per start."""
    fields: Dict[str, Any] = {
        "version": _diagnostics_application_version(),
        "python": platform.python_version(),
        "os": platform.platform(),
        "frozen": _diagnostics_is_frozen(),
    }
    requirements = _diagnostics_runtime_requirements()
    if requirements:
        fields["requires"] = "python>={} tk>={}".format(
            requirements.get("python_text", "?"), requirements.get("tk_text", "?")
        )
    return fields


def diagnostics_start() -> Optional[Path]:
    """Start the diagnostic session (idempotent) and return the active log path.

    The first writable candidate wins: the documented per-user directory, then a
    temporary fallback that is disclosed in the log and in
    ``diagnostics_recovery_text()``. Returns ``None`` when neither can be written.
    """
    with _state.lock:
        if _state.started:
            return _state.path
        _state.started = True
        _state.session_id = uuid.uuid4().hex[:8]
        _state.write_failure_noted = False
        _state.last_error = ""

        logger = logging.getLogger(DIAGNOSTICS_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _state.logger = logger

        for locate_path, is_fallback, location in (
            (_diagnostics_standard_log_path, False, "per-user log"),
            (_diagnostics_fallback_log_path, True, "temporary log"),
        ):
            try:
                # Path.home() can fail in a stripped Windows environment.
                # Resolve each candidate lazily so that failure still reaches
                # the temporary fallback, and never prevents application boot.
                path = locate_path()
                handler = _diagnostics_build_file_handler(path)
            except (OSError, RuntimeError) as error:
                _state.last_error = "{}: {}".format(location, error)
                continue
            logger.addHandler(handler)
            _state.file_handler = handler
            _state.path = path
            _state.fallback = is_fallback
            break

        stream = sys.stderr
        if stream is not None:
            stream_handler = _DiagnosticsStreamHandler(stream)
            stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
            stream_handler.addFilter(_DiagnosticsFilter())
            logger.addHandler(stream_handler)
            _state.stream_handler = stream_handler

    if _state.path is not None:
        diagnostics_info("Diagnostic session started", **_diagnostics_session_fields())
        if _state.fallback:
            diagnostics_info(
                "Diagnostic log fallback in use",
                path=str(_state.path),
                reason=_state.last_error,
            )
    else:
        _diagnostics_write_stderr(
            "Diagnostic log unavailable: {}. The application continues, but no "
            "log file will be written.".format(_state.last_error or "no writable location")
        )
    return _state.path


def diagnostics_log_path() -> Optional[Path]:
    """Return the active log file, or ``None`` when no file is being written."""
    with _state.lock:
        if not _state.started or _state.path is None:
            return None
        return _state.path


def _diagnostics_active_logger() -> Optional[logging.Logger]:
    """Return the session logger, if a session is active."""
    with _state.lock:
        return _state.logger


def _diagnostics_emit(level: int, text: str) -> None:
    """Write one record without ever raising into the caller."""
    logger = _diagnostics_active_logger()
    if logger is not None:
        try:
            logger.log(level, text)
        except Exception:  # noqa: BLE001 - logging must never break the caller
            _diagnostics_note_write_failure()
        return
    _diagnostics_write_stderr("{} {}".format(logging.getLevelName(level), text))


def diagnostics_info(message: str, **fields: Any) -> None:
    """Record an informational entry (mirrored to stderr when one exists)."""
    _diagnostics_emit(logging.INFO, _diagnostics_compose(message, fields))


def diagnostics_exception(message: str, error: Optional[BaseException] = None) -> None:
    """Record an error and a traceback.

    When ``error`` is supplied its ``__traceback__`` is serialized, which is what
    a queued or previously caught error carries. Without it the current
    ``sys.exc_info()`` is used, so the call works inside an ``except`` block.
    """
    detail = ""
    if error is not None:
        try:
            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        except Exception:  # noqa: BLE001 - tracing must never raise
            detail = "{}: {}".format(type(error).__name__, error)
    else:
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_value is not None:
            try:
                detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            except Exception:  # noqa: BLE001 - tracing must never raise
                detail = str(exc_value)
    text = _diagnostics_bounded_message(message)
    if detail.strip():
        text += "\n" + detail.strip()[:_MAX_TRACEBACK_CHARS]
    _diagnostics_emit(logging.ERROR, text)


def diagnostics_open_log() -> bool:
    """Open the active log with the platform's own viewer; return success."""
    path = diagnostics_log_path()
    if path is None or not path.exists():
        return False
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(
                ["/usr/bin/open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform.startswith("win"):
            opener = getattr(os, "startfile", None)
            if opener is None:
                return False
            opener(str(path))
        else:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:  # noqa: BLE001 - opening a log is best effort
        return False
    return True


def _diagnostics_restore_hooks() -> None:
    """Restore the exception hooks that were replaced, idempotently."""
    with _state.lock:
        if not _state.hooks_installed:
            return
        _state.hooks_installed = False
        if _state.previous_sys_hook is not None:
            sys.excepthook = _state.previous_sys_hook
        if _state.previous_thread_hook is not None:
            threading.excepthook = _state.previous_thread_hook
        _state.previous_sys_hook = None
        _state.previous_thread_hook = None


def _diagnostics_sys_excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    """Log an uncaught exception, then run the hook that was previously installed."""
    try:
        diagnostics_exception("Uncaught exception", exc_value)
    except Exception:  # noqa: BLE001 - hooks must never raise
        pass
    previous = _state.previous_sys_hook
    if previous is not None:
        try:
            previous(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001 - the previous hook owns its failures
            pass


def _diagnostics_thread_excepthook(args: Any) -> None:
    """Log an uncaught worker-thread exception, then defer to the previous hook."""
    try:
        name = getattr(getattr(args, "thread", None), "name", "unknown")
        diagnostics_exception("Uncaught exception in thread {}".format(name), getattr(args, "exc_value", None))
    except Exception:  # noqa: BLE001 - hooks must never raise
        pass
    previous = _state.previous_thread_hook
    if previous is not None:
        try:
            previous(args)
        except Exception:  # noqa: BLE001 - the previous hook owns its failures
            pass


def diagnostics_install_exception_hooks() -> None:
    """Install sys/thread hooks that record true uncaught errors (idempotent)."""
    with _state.lock:
        if _state.hooks_installed:
            return
        _state.previous_sys_hook = sys.excepthook
        _state.previous_thread_hook = getattr(threading, "excepthook", None)
        sys.excepthook = _diagnostics_sys_excepthook
        if _state.previous_thread_hook is not None:
            threading.excepthook = _diagnostics_thread_excepthook
        _state.hooks_installed = True


def diagnostics_shutdown() -> None:
    """Restore hooks and close the handlers owned by this module (idempotent)."""
    _diagnostics_restore_hooks()
    with _state.lock:
        logger = _state.logger
        if logger is not None:
            for handler in [item for item in logger.handlers if item in (_state.file_handler, _state.stream_handler)]:
                try:
                    handler.flush()
                except Exception:  # noqa: BLE001 - shutdown must never raise
                    pass
                try:
                    logger.removeHandler(handler)
                except Exception:  # noqa: BLE001 - shutdown must never raise
                    pass
                try:
                    handler.close()
                except Exception:  # noqa: BLE001 - shutdown must never raise
                    pass
        _state.started = False
        _state.logger = None
        _state.file_handler = None
        _state.stream_handler = None
        _state.path = None
        _state.fallback = False
        _state.last_error = ""
        _state.session_id = ""
        _state.write_failure_noted = False


def diagnostics_recovery_text() -> str:
    """Return source-vs-packaged recovery guidance including the actual log path.

    The Python/Tk minimums come from the runtime module's getter so this text
    never duplicates policy.
    """
    path = diagnostics_log_path()
    if path is not None:
        location = "The diagnostic log is at {}".format(path)
        if _state.fallback:
            location += " (temporary fallback location)"
        location += "."
    else:
        try:
            location = "No diagnostic log could be created; check write access to {}.".format(
                _diagnostics_standard_log_path().parent
            )
        except (OSError, RuntimeError):
            location = "No diagnostic log could be created; check the user profile and temporary directory."

    requirements = _diagnostics_runtime_requirements()
    if _diagnostics_is_frozen():
        guidance = (
            "This is the packaged application, so Python and Tk are included: "
            "install the latest version again from a fresh download and try once more."
        )
    elif requirements:
        # Version numbers only: the launcher owns the install commands, so this
        # text cannot drift from runtime_get_requirements() when policy changes.
        guidance = (
            "Install or update Python to {}+ and Tk to {}+, then run the "
            "launcher again."
        ).format(requirements.get("python_text", "?"), requirements.get("tk_text", "?"))
    else:
        guidance = "Install or update the launcher runtime, then try again."
    return "{} {}".format(guidance, location)
