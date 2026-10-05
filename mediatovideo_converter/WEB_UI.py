"""Tkinter graphical interface for Mediatovideo Converter.

The UI talks to scanner and converter modules only through their public APIs.
Worker threads communicate through a queue so Tk is never updated off-thread.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __version__
from . import diagnostics
from .converter import (
    FFmpegNotFoundError,
    converter_collect_mp4s,
    converter_convert,
    converter_convert_mkv_to_mp4,
    converter_find_tools,
    converter_join_mp4s,
    converter_order_creation_key,
    converter_order_folder_key,
    converter_order_time_key,
)
from .error_messages import error_messages_format, error_messages_format_operation
from .models import (
    ConversionOptions,
    ConversionSummary,
    GroupingMode,
    NamingMode,
    OutputLayout,
    ScanResult,
    VideoFormat,
)
from .scanner import ScanCancelled, scanner_scan

_GROUPING_LABELS = {
    "One video per day": GroupingMode.DAY,
    "One video per child folder in each day": GroupingMode.CHILD_FOLDER,
}
_LAYOUT_LABELS = {
    "Replicate YYYY/MM/DD folders": OutputLayout.MIRROR_DATES,
    "Put all videos in one folder": OutputLayout.FLAT,
}
_FORMAT_LABELS = {
    "MKV — fast, lossless stream copy": VideoFormat.MKV_COPY,
    "MP4 — compatible H.264 (slower)": VideoFormat.MP4_H264,
}
_NAMING_LABELS = {
    "Month-Day (07-17)": NamingMode.MONTH_DAY,
    "Month-Day-Category (07-17-Camera1)": NamingMode.MONTH_DAY_CATEGORY,
}
_MAX_UI_MESSAGES = 5000
_MAX_LOG_LINES = 2000
_READY_MESSAGE = (
    "Application window initialized; the application should be running now."
)


def _diagnostics_info(message: str, **fields: object) -> None:
    """Mirror a message to the persistent log without risking the interface."""

    try:
        diagnostics.diagnostics_info(message, **fields)
    except Exception:  # noqa: BLE001 - logging must never break the interface
        pass


def _diagnostics_exception(message: str, error: BaseException) -> None:
    """Write a caught failure and its traceback without risking the interface."""

    try:
        diagnostics.diagnostics_exception(message, error)
    except Exception:  # noqa: BLE001 - logging must never break the interface
        pass


def _diagnostics_log_path() -> Path | None:
    """Return the active log path without letting the logger break the UI."""

    try:
        return diagnostics.diagnostics_log_path()
    except Exception:  # noqa: BLE001 - logging must never break the interface
        return None


def _diagnostics_recovery_text() -> str:
    """Return the recovery guidance without letting the logger break the UI."""

    try:
        return diagnostics.diagnostics_recovery_text()
    except Exception:  # noqa: BLE001 - logging must never break the interface
        return ""


def _install_macos_quit_hook(
    root: object, handler: Callable[[], None], platform_name: str | None = None
) -> bool:
    """Route the macOS App menu Quit through the normal close path.

    Tk dispatches the App menu Quit command inside its native menu stack, where
    opening a confirmation dialog can hang the application on macOS 27, so the
    command only queues the existing close handler with ``after_idle`` and
    returns immediately (workaround from CPython issue 158053). Other platforms
    are untouched, and no menu of our own is added.
    """

    if (platform_name or sys.platform) != "darwin":
        return False
    try:
        root.createcommand("tk::mac::Quit", handler)
    except tk.TclError:
        return False
    return True


class MkvToMp4Dialog:
    """Modal single-file MKV-to-MP4 conversion interface."""

    def __init__(
        self, parent: tk.Tk, ffmpeg_directory: Path | None = None
    ) -> None:
        self._parent = parent
        self._ffmpeg_directory = ffmpeg_directory
        self._messages: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=500)
        self._cancel_event = threading.Event()
        self._busy = False
        self._close_when_done = False
        self._completed_output: Path | None = None

        self._source = tk.StringVar()
        self._target = tk.StringVar()
        self._status = tk.StringVar(
            value="Choose an MKV file and where its MP4 copy should be saved."
        )
        self._progress_detail = tk.StringVar(value="Waiting")

        self._window = tk.Toplevel(parent)
        self._window.title("MKV to MP4 — Mediatovideo Converter")
        self._window.geometry("720x390")
        self._window.minsize(620, 360)
        self._window.transient(parent)
        self._window.grab_set()
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_interface()
        self._window.after(100, self._poll_messages)

    def _build_interface(self) -> None:
        """Create file selectors, progress, and conversion controls."""

        outer = ttk.Frame(self._window, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(
            outer,
            text="Convert one MKV file to MP4",
            font=("TkDefaultFont", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Video is encoded as H.264 and available audio as AAC for compatibility.",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        files = ttk.LabelFrame(outer, text="Input and output files", padding=10)
        files.grid(row=2, column=0, sticky="ew")
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text="MKV input").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self._source_entry = ttk.Entry(files, textvariable=self._source)
        self._source_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self._source_button = ttk.Button(
            files, text="Browse…", command=self._choose_source
        )
        self._source_button.grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(files, text="MP4 output").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self._target_entry = ttk.Entry(files, textvariable=self._target)
        self._target_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self._target_button = ttk.Button(
            files, text="Browse…", command=self._choose_target
        )
        self._target_button.grid(row=1, column=2, padx=(8, 0), pady=4)

        progress = ttk.LabelFrame(outer, text="Conversion progress", padding=10)
        progress.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        progress.columnconfigure(0, weight=1)
        ttk.Label(progress, textvariable=self._progress_detail).grid(
            row=0, column=0, sticky="w"
        )
        self._progress = ttk.Progressbar(
            progress, mode="determinate", maximum=100, value=0
        )
        self._progress.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        ttk.Label(
            outer,
            textvariable=self._status,
            wraplength=660,
            anchor="w",
            justify=tk.LEFT,
        ).grid(row=4, column=0, sticky="ew", pady=(12, 0))

        controls = ttk.Frame(outer)
        controls.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        controls.columnconfigure(3, weight=1)
        self._convert_button = ttk.Button(
            controls, text="Convert to MP4", command=self._start_conversion
        )
        self._convert_button.grid(row=0, column=0, padx=(0, 8))
        self._cancel_button = ttk.Button(
            controls, text="Cancel", command=self._cancel, state=tk.DISABLED
        )
        self._cancel_button.grid(row=0, column=1)
        self._open_button = ttk.Button(
            controls,
            text="Open output folder",
            command=self._open_output_folder,
            state=tk.DISABLED,
        )
        self._open_button.grid(row=0, column=4, padx=(8, 0))

    def _choose_source(self) -> None:
        """Choose an MKV and suggest a collision-free MP4 filename."""

        selected = filedialog.askopenfilename(
            parent=self._window,
            title="Choose an MKV file",
            filetypes=(("Matroska video", "*.mkv"), ("All files", "*.*")),
        )
        if not selected:
            self._status.set("MKV selection cancelled; no file changed.")
            return
        source = Path(selected)
        self._source.set(str(source))
        target = source.with_suffix(".mp4")
        if target.exists():
            target = source.with_name(f"{source.stem}-converted.mp4")
            number = 2
            while target.exists():
                target = source.with_name(f"{source.stem}-converted-{number}.mp4")
                number += 1
        self._target.set(str(target))
        self._status.set("MKV selected. Review the MP4 destination, then convert.")

    def _choose_target(self) -> None:
        """Choose the destination MP4 path."""

        source_text = self._source.get().strip()
        source = Path(source_text).expanduser() if source_text else None
        selected = filedialog.asksaveasfilename(
            parent=self._window,
            title="Save converted MP4 as",
            defaultextension=".mp4",
            filetypes=(("MP4 video", "*.mp4"), ("All files", "*.*")),
            initialdir=str(source.parent) if source else None,
            initialfile=f"{source.stem}.mp4" if source else "converted.mp4",
        )
        if selected:
            self._target.set(selected)
            self._status.set("MP4 destination selected.")
        else:
            self._status.set("MP4 destination selection cancelled; no file changed.")

    def _start_conversion(self) -> None:
        """Validate visible fields and start conversion off the Tk thread."""

        if self._busy:
            self._status.set("The MKV conversion is already running.")
            return
        source_text = self._source.get().strip()
        target_text = self._target.get().strip()
        if not source_text:
            self._show_error(
                error_messages_format(
                    "Checking MKV input",
                    "No MKV input file has been selected.",
                    "Choose an MKV file, then try again.",
                )
            )
            return
        if not target_text:
            self._show_error(
                error_messages_format(
                    "Checking MP4 output",
                    "No MP4 output filename has been selected.",
                    "Choose where the MP4 should be saved, then try again.",
                )
            )
            return

        source = Path(source_text)
        target = Path(target_text)
        self._set_busy(True)
        self._cancel_event.clear()
        self._completed_output = None
        self._progress.stop()
        self._progress.configure(mode="indeterminate", value=0)
        self._progress.start(12)
        self._progress_detail.set("Checking MKV and locating FFmpeg…")
        self._status.set("Conversion started. Progress will update below.")
        _diagnostics_info(
            "MKV to MP4 conversion started",
            source=str(source),
            target=str(target),
        )

        def worker() -> None:
            try:
                result = converter_convert_mkv_to_mp4(
                    source,
                    target,
                    ffmpeg_directory=self._ffmpeg_directory,
                    event=lambda name, details: self._queue_event(name, details),
                    cancel_event=self._cancel_event,
                )
            except Exception as error:
                # The traceback is written here, at the catch, so delivering the
                # error to the interface does not log it a second time.
                _diagnostics_exception("MKV to MP4 conversion failed", error)
                self._messages.put(("error", error))
            else:
                self._messages.put(("done", result))

        threading.Thread(target=worker, name="mkv-to-mp4", daemon=True).start()

    def _queue_event(self, name: str, details: dict[str, object]) -> None:
        """Queue progress events while allowing redundant updates to be dropped."""

        try:
            self._messages.put_nowait(("event", name, details))
        except queue.Full:
            pass

    def _poll_messages(self) -> None:
        """Render worker messages safely on Tk's thread."""

        try:
            while True:
                message = self._messages.get_nowait()
                if message[0] == "event":
                    self._handle_event(message[1], message[2])
                elif message[0] == "error":
                    self._finish_error(message[1])
                elif message[0] == "done":
                    self._finish_conversion(message[1])
        except queue.Empty:
            pass
        try:
            if self._window.winfo_exists():
                self._window.after(100, self._poll_messages)
        except tk.TclError:
            return

    def _handle_event(self, name: str, details: dict[str, object]) -> None:
        """Display converter progress events."""

        if name == "single_started":
            self._progress_detail.set(f"Converting {Path(details['source']).name}…")
        elif name == "encoding_progress":
            fraction = details.get("fraction")
            if fraction is None:
                self._progress_detail.set("Encoding MP4… duration unavailable")
                return
            self._progress.stop()
            self._progress.configure(
                mode="determinate", maximum=100, value=float(fraction) * 100
            )
            self._progress_detail.set(f"Encoding MP4… {float(fraction) * 100:.1f}%")

    def _finish_conversion(self, result: Any) -> None:
        """Show cancellation or successful completion."""

        self._progress.stop()
        self._set_busy(False)
        if result.cancelled:
            _diagnostics_info("MKV to MP4 conversion cancelled")
            self._progress.configure(mode="determinate", value=0)
            self._progress_detail.set("Conversion cancelled")
            self._status.set("MKV to MP4 conversion was cancelled safely.")
        else:
            _diagnostics_info(
                "MKV to MP4 conversion completed", output=str(result.output)
            )
            self._completed_output = result.output
            self._progress.configure(mode="determinate", maximum=100, value=100)
            self._progress_detail.set("Conversion complete")
            self._status.set(f"MP4 created successfully: {result.output}")
            self._open_button.configure(state=tk.NORMAL)
            messagebox.showinfo(
                "MKV conversion complete",
                f"The MP4 was created successfully.\n\nOutput: {result.output}",
                parent=self._window,
            )
        if self._close_when_done:
            self._destroy()

    def _finish_error(self, error: BaseException) -> None:
        """Restore controls and show a structured conversion error."""

        self._progress.stop()
        self._progress.configure(mode="determinate", value=0)
        self._progress_detail.set("Conversion failed")
        self._set_busy(False)
        text = str(error)
        details = (
            text
            if text.startswith("Stage:")
            else error_messages_format_operation("Converting MKV to MP4", error)
        )
        self._status.set("The MKV could not be converted. See the error dialog.")
        self._show_error(details)
        if self._close_when_done:
            self._destroy()

    def _show_error(self, details: str) -> None:
        """Show an error in both the window and a readable dialog."""

        self._status.set(details.replace("\n", " "))
        messagebox.showerror("MKV to MP4 error", details, parent=self._window)

    def _set_busy(self, busy: bool) -> None:
        """Enable only actions that are safe for the current state."""

        self._busy = busy
        normal_state = tk.DISABLED if busy else tk.NORMAL
        self._source_entry.configure(state=normal_state)
        self._target_entry.configure(state=normal_state)
        self._source_button.configure(state=normal_state)
        self._target_button.configure(state=normal_state)
        self._convert_button.configure(state=normal_state)
        self._cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)

    def _cancel(self) -> None:
        """Request a safe FFmpeg cancellation and acknowledge immediately."""

        if not self._busy:
            self._status.set("There is no MKV conversion running to cancel.")
            return
        self._cancel_event.set()
        self._cancel_button.configure(state=tk.DISABLED)
        self._status.set("Cancellation requested; waiting for FFmpeg to stop safely…")
        _diagnostics_info("MKV to MP4 cancellation requested")

    def _open_output_folder(self) -> None:
        """Open the folder containing the completed MP4."""

        if not self._completed_output:
            self._status.set("No completed MP4 is available to open yet.")
            return
        folder = self._completed_output.parent
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            self._show_error(
                error_messages_format_operation("Opening MP4 output folder", error)
            )
        else:
            self._status.set(f"Opened output folder: {folder}")

    def _on_close(self) -> None:
        """Protect a running conversion when the dialog is closed."""

        if self._busy:
            if not messagebox.askyesno(
                "Conversion running",
                "Cancel the MKV conversion and close this window?",
                parent=self._window,
            ):
                self._status.set("MKV conversion is continuing.")
                return
            self._close_when_done = True
            self._cancel()
            return
        self._destroy()

    def _destroy(self) -> None:
        """Release the modal grab and close the dialog."""

        try:
            self._window.grab_release()
        except tk.TclError:
            pass
        self._window.destroy()


class JoinMp4Dialog:
    """Modal folder-wide video joining interface with visible order control."""

    def __init__(
        self, parent: tk.Tk, ffmpeg_directory: Path | None = None
    ) -> None:
        self._parent = parent
        self._ffmpeg_directory = ffmpeg_directory
        self._files: list[Path] = []
        self._messages: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=500)
        self._cancel_event = threading.Event()
        self._busy = False
        self._close_when_done = False
        self._completed_output: Path | None = None

        self._folder = tk.StringVar()
        self._target = tk.StringVar()
        self._status = tk.StringVar(
            value="Choose a folder of MP4 or MKV pieces made by this app."
        )
        self._progress_detail = tk.StringVar(value="Waiting")

        self._window = tk.Toplevel(parent)
        self._window.title("Join videos — Mediatovideo Converter")
        self._window.geometry("720x560")
        self._window.minsize(620, 480)
        self._window.transient(parent)
        self._window.grab_set()
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_interface()
        self._window.after(100, self._poll_messages)

    def _build_interface(self) -> None:
        """Create folder picker, order list, and join controls."""

        outer = ttk.Frame(self._window, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text="Join video pieces into one video",
            font=("TkDefaultFont", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Picks every MP4 or MKV in a folder (subfolders included), "
            "orders them by time, and copies them into one video.",
            wraplength=660,
            justify=tk.LEFT,
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        picker = ttk.Frame(outer)
        picker.grid(row=2, column=0, sticky="nsew")
        picker.columnconfigure(0, weight=1)
        picker.rowconfigure(2, weight=1)

        folder_row = ttk.Frame(picker)
        folder_row.grid(row=0, column=0, sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self._folder, state="readonly").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            folder_row, text="Choose folder…", command=self._choose_folder
        ).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(picker, text="Join order (top joins first):").grid(
            row=1, column=0, sticky="w", pady=(10, 4)
        )
        list_frame = ttk.Frame(picker)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self._list = tk.Listbox(list_frame, height=8)
        self._list.grid(row=0, column=0, sticky="nsew")
        self._list.bind("<<ListboxSelect>>", self._on_select)
        list_scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self._list.yview
        )
        list_scroll.grid(row=0, column=1, sticky="ns")
        self._list.configure(yscrollcommand=list_scroll.set)

        order_row = ttk.Frame(picker)
        order_row.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(order_row, text="Time order", command=self._sort_time).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(
            order_row, text="Original order", command=self._sort_folder
        ).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(
            order_row, text="Creation order", command=self._sort_creation
        ).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(order_row, text="Up", command=self._move_up).grid(
            row=0, column=3, padx=(0, 8)
        )
        ttk.Button(order_row, text="Down", command=self._move_down).grid(
            row=0, column=4
        )

        target_row = ttk.Frame(outer)
        target_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        target_row.columnconfigure(0, weight=1)
        ttk.Label(target_row, text="Joined output:").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        target_entry_row = ttk.Frame(target_row)
        target_entry_row.grid(row=1, column=0, sticky="ew")
        target_entry_row.columnconfigure(0, weight=1)
        self._target_entry = ttk.Entry(
            target_entry_row, textvariable=self._target
        )
        self._target_entry.grid(row=0, column=0, sticky="ew")
        self._target_button = ttk.Button(
            target_entry_row, text="Browse…", command=self._choose_target
        )
        self._target_button.grid(row=0, column=1, padx=(8, 0))

        progress = ttk.LabelFrame(outer, text="Join progress", padding=10)
        progress.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        progress.columnconfigure(0, weight=1)
        ttk.Label(progress, textvariable=self._progress_detail).grid(
            row=0, column=0, sticky="w"
        )
        self._progress = ttk.Progressbar(
            progress, mode="determinate", maximum=100, value=0
        )
        self._progress.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        ttk.Label(
            outer,
            textvariable=self._status,
            wraplength=660,
            anchor="w",
            justify=tk.LEFT,
        ).grid(row=5, column=0, sticky="ew", pady=(12, 0))

        controls = ttk.Frame(outer)
        controls.grid(row=6, column=0, sticky="ew", pady=(14, 0))
        controls.columnconfigure(3, weight=1)
        self._join_button = ttk.Button(
            controls, text="Join videos", command=self._start_join
        )
        self._join_button.grid(row=0, column=0, padx=(0, 8))
        self._cancel_button = ttk.Button(
            controls, text="Cancel", command=self._cancel, state=tk.DISABLED
        )
        self._cancel_button.grid(row=0, column=1)
        self._open_button = ttk.Button(
            controls,
            text="Open output folder",
            command=self._open_output_folder,
            state=tk.DISABLED,
        )
        self._open_button.grid(row=0, column=4, padx=(8, 0))

    def _choose_folder(self) -> None:
        """Collect every MP4/MKV below the chosen folder in time order."""

        folder = filedialog.askdirectory(title="Choose a folder of video pieces")
        if not folder:
            self._status.set("Folder selection cancelled; no folder changed.")
            return
        try:
            found = converter_collect_mp4s(Path(folder), (".mp4", ".mkv"))
        except Exception as error:
            self._show_error(str(error))
            return
        self._folder.set(folder)
        if not found:
            self._files = []
            self._refresh_list()
            self._status.set("No MP4 or MKV files found below the selected folder.")
            return
        self._files = sorted(found, key=converter_order_time_key)
        self._refresh_list()
        self._suggest_target(Path(folder))
        self._status.set(
            f"Found {len(self._files)} video(s) in time order. "
            "Check the list, then join."
        )

    def _suggest_target(self, folder: Path) -> None:
        """Suggest a collision-free joined filename beside the folder."""

        stem = f"{folder.name}-joined"
        candidate = folder.parent / f"{stem}.mp4"
        number = 2
        while candidate.exists():
            candidate = folder.parent / f"{stem}-{number}.mp4"
            number += 1
        self._target.set(str(candidate))

    def _refresh_list(self) -> None:
        """Redraw the visible join order from the current file list."""

        self._list.delete(0, tk.END)
        for index, path in enumerate(self._files, start=1):
            self._list.insert(tk.END, f"{index}. {path.name}")

    def _sort_time(self) -> None:
        """Restore automatic time order."""

        self._files.sort(key=converter_order_time_key)
        self._refresh_list()
        self._status.set("List sorted into time order.")

    def _sort_folder(self) -> None:
        """Restore original folder-discovery order."""

        self._files.sort(key=converter_order_folder_key)
        self._refresh_list()
        self._status.set("List restored to original folder order.")

    def _sort_creation(self) -> None:
        """Order by file creation time, oldest first.

        Use this when filenames carry no event time (Month-Day naming):
        pieces are written in event order, so creation order is time order
        as long as the files were never moved or copied.
        """

        self._files.sort(key=converter_order_creation_key)
        self._refresh_list()
        self._status.set("List sorted into creation order (oldest first).")

    def _move_up(self) -> None:
        """Move the selected row one place earlier."""

        selection = self._list.curselection()
        if not selection or selection[0] == 0:
            return
        index = selection[0]
        self._files[index - 1], self._files[index] = (
            self._files[index],
            self._files[index - 1],
        )
        self._refresh_list()
        self._list.selection_set(index - 1)

    def _move_down(self) -> None:
        """Move the selected row one place later."""

        selection = self._list.curselection()
        if not selection or selection[0] >= len(self._files) - 1:
            return
        index = selection[0]
        self._files[index + 1], self._files[index] = (
            self._files[index],
            self._files[index + 1],
        )
        self._refresh_list()
        self._list.selection_set(index + 1)

    def _on_select(self, _event: object) -> None:
        """Show the full path of the selected row."""

        selection = self._list.curselection()
        if selection:
            self._status.set(str(self._files[selection[0]]))

    def _choose_target(self) -> None:
        """Choose where the joined video should be saved."""

        folder_text = self._folder.get().strip()
        folder = Path(folder_text).expanduser() if folder_text else None
        selected = filedialog.asksaveasfilename(
            parent=self._window,
            title="Save joined video as",
            defaultextension=".mp4",
            filetypes=(
                ("MP4 video", "*.mp4"),
                ("MKV video", "*.mkv"),
                ("All files", "*.*"),
            ),
            initialdir=str(folder.parent) if folder else None,
            initialfile=f"{folder.name}-joined.mp4" if folder else "joined.mp4",
        )
        if selected:
            self._target.set(selected)
            self._status.set("Joined output selected.")
        else:
            self._status.set("Output selection cancelled; no file changed.")

    def _start_join(self) -> None:
        """Validate the visible list and join off the Tk thread."""

        if self._busy:
            self._status.set("The join is already running.")
            return
        if len(self._files) < 2:
            self._show_error(
                error_messages_format(
                    "Checking video inputs",
                    "At least two video files are needed for a join.",
                    "Choose a folder containing converted pieces, then try again.",
                )
            )
            return
        target_text = self._target.get().strip()
        if not target_text:
            self._show_error(
                error_messages_format(
                    "Checking join output",
                    "No joined output filename has been selected.",
                    "Choose where the joined video should be saved, then try again.",
                )
            )
            return
        ordered = list(self._files)
        target = Path(target_text)
        self._set_busy(True)
        self._cancel_event.clear()
        self._completed_output = None
        self._progress.configure(mode="indeterminate", value=0)
        self._progress.start(12)
        self._progress_detail.set(f"Joining {len(ordered)} video(s)…")
        self._status.set(
            "Join started. First file joins first — check the list order."
        )

        def worker() -> None:
            try:
                result = converter_join_mp4s(
                    ordered,
                    target,
                    ffmpeg_directory=self._ffmpeg_directory,
                    event=lambda name, details: self._queue_event(name, details),
                    cancel_event=self._cancel_event,
                )
            except Exception as error:
                self._messages.put(("error", error))
            else:
                self._messages.put(("done", result))

        threading.Thread(target=worker, name="mp4-join", daemon=True).start()

    def _queue_event(self, name: str, details: dict[str, object]) -> None:
        """Queue progress events while allowing redundant updates to be dropped."""

        try:
            self._messages.put_nowait(("event", name, details))
        except queue.Full:
            pass

    def _poll_messages(self) -> None:
        """Render worker messages safely on Tk's thread."""

        try:
            while True:
                message = self._messages.get_nowait()
                if message[0] == "event":
                    self._handle_event(message[1], message[2])
                elif message[0] == "error":
                    self._finish_error(message[1])
                elif message[0] == "done":
                    self._finish_join(message[1])
        except queue.Empty:
            pass
        try:
            if self._window.winfo_exists():
                self._window.after(100, self._poll_messages)
        except tk.TclError:
            return

    def _handle_event(self, name: str, details: dict[str, object]) -> None:
        """Display join progress events."""

        if name == "single_started":
            sources = details.get("sources", [])
            count = len(sources) if isinstance(sources, list) else "?"
            self._progress_detail.set(f"Joining {count} video(s)…")
        elif name == "encoding_progress":
            fraction = details.get("fraction")
            if fraction is None:
                self._progress_detail.set("Joining videos…")
                return
            self._progress.stop()
            self._progress.configure(
                mode="determinate", maximum=100, value=float(fraction) * 100
            )
            self._progress_detail.set(
                f"Joining videos… {float(fraction) * 100:.1f}%"
            )

    def _finish_join(self, result: Any) -> None:
        """Show cancellation or successful completion."""

        self._progress.stop()
        self._set_busy(False)
        if result.cancelled:
            self._progress.configure(mode="determinate", value=0)
            self._progress_detail.set("Join cancelled")
            self._status.set("Video join was cancelled safely.")
        else:
            self._completed_output = result.output
            self._progress.configure(mode="determinate", maximum=100, value=100)
            self._progress_detail.set("Join complete")
            self._status.set(f"Joined video created: {result.output}")
            self._open_button.configure(state=tk.NORMAL)
            messagebox.showinfo(
                "Join complete",
                f"The joined video was created successfully.\n\nOutput: {result.output}",
                parent=self._window,
            )
        if self._close_when_done:
            self._destroy()

    def _finish_error(self, error: BaseException) -> None:
        """Restore controls and show a structured join error."""

        self._progress.stop()
        self._progress.configure(mode="determinate", value=0)
        self._progress_detail.set("Join failed")
        self._set_busy(False)
        text = str(error)
        details = (
            text
            if text.startswith("Stage:")
            else error_messages_format_operation("Joining videos", error)
        )
        self._status.set("The videos could not be joined. See the error dialog.")
        self._show_error(details)
        if self._close_when_done:
            self._destroy()

    def _show_error(self, details: str) -> None:
        """Show an error in both the window and a readable dialog."""

        self._status.set(details.replace("\n", " "))
        messagebox.showerror("Join videos error", details, parent=self._window)

    def _set_busy(self, busy: bool) -> None:
        """Enable only actions that are safe for the current state."""

        self._busy = busy
        normal_state = tk.DISABLED if busy else tk.NORMAL
        self._target_entry.configure(state=normal_state)
        self._target_button.configure(state=normal_state)
        self._join_button.configure(state=normal_state)
        self._cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)

    def _cancel(self) -> None:
        """Request a safe FFmpeg cancellation and acknowledge immediately."""

        if not self._busy:
            self._status.set("There is no join running to cancel.")
            return
        self._cancel_event.set()
        self._cancel_button.configure(state=tk.DISABLED)
        self._status.set("Cancellation requested; waiting for FFmpeg to stop safely…")

    def _open_output_folder(self) -> None:
        """Open the folder containing the joined video."""

        if not self._completed_output:
            self._status.set("No joined video is available to open yet.")
            return
        folder = self._completed_output.parent
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            self._show_error(
                error_messages_format_operation("Opening joined output folder", error)
            )
        else:
            self._status.set(f"Opened output folder: {folder}")

    def _on_close(self) -> None:
        """Protect a running join when the dialog is closed."""

        if self._busy:
            if not messagebox.askyesno(
                "Join running",
                "Cancel the video join and close this window?",
                parent=self._window,
            ):
                self._status.set("Video join is continuing.")
                return
            self._close_when_done = True
            self._cancel()
            return
        self._destroy()

    def _destroy(self) -> None:
        """Release the modal grab and close the dialog."""

        try:
            self._window.grab_release()
        except tk.TclError:
            pass
        self._window.destroy()


class MediaToVideoApp:
    """Own and coordinate the desktop interface."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._messages: queue.Queue[tuple[Any, ...]] = queue.Queue(
            maxsize=_MAX_UI_MESSAGES
        )
        self._cancel_event = threading.Event()
        self._scan_result: ScanResult | None = None
        self._busy_operation: str | None = None
        self._log_line_count = 0
        self._ready_announced = False

        self._source = tk.StringVar()
        self._output = tk.StringVar()
        self._ffmpeg_directory = tk.StringVar()
        self._grouping = tk.StringVar(value=next(iter(_GROUPING_LABELS)))
        self._layout = tk.StringVar(value=next(iter(_LAYOUT_LABELS)))
        self._format = tk.StringVar(value=next(iter(_FORMAT_LABELS)))
        self._naming = tk.StringVar(value=next(iter(_NAMING_LABELS)))
        self._status = tk.StringVar(value="Choose a source folder, then scan it.")
        self._scan_detail = tk.StringVar(value="Not scanned")
        self._overall_detail = tk.StringVar(value="No conversion running")
        self._current_detail = tk.StringVar(value="Waiting")

        self._configure_window()
        self._build_interface()
        self._source.trace_add("write", self._source_or_grouping_changed)
        self._grouping.trace_add("write", self._source_or_grouping_changed)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Tk callback failures have no console in a windowed build.
        self._root.report_callback_exception = self._report_callback_exception
        # The application defines no menus of its own. Only the macOS App menu
        # Quit needs routing: it is deferred so its confirmation cannot run in
        # the native menu stack. About, Preferences, and Help stay at Tk's own
        # defaults, which the real-menu check confirms open no modal dialog.
        _install_macos_quit_hook(self._root, self._on_native_quit)
        # The window is only "running" once it has actually mapped and drawn.
        self._root.bind("<Map>", self._announce_ready, add="+")
        self._root.bind("<Configure>", self._announce_ready, add="+")
        self._root.after_idle(self._announce_ready)
        self._root.after(100, self._poll_messages)

    def _configure_window(self) -> None:
        """Set cross-platform window defaults."""

        self._root.title(f"Mediatovideo Converter {__version__}")
        self._root.minsize(760, 720)
        self._root.geometry("900x780")
        try:
            ttk.Style().theme_use("aqua" if sys.platform == "darwin" else "vista")
        except tk.TclError:
            pass

    def _build_interface(self) -> None:
        """Create all controls and visible progress surfaces."""

        outer = ttk.Frame(self._root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        title = ttk.Label(
            outer,
            text="Mediatovideo Converter",
            font=("TkDefaultFont", 18, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Combine camera .media clips into usable MKV or MP4 videos.",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        paths = ttk.LabelFrame(outer, text="Folders and FFmpeg", padding=10)
        paths.grid(row=2, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._add_path_row(paths, 0, "Source", self._source, self._choose_source)
        self._add_path_row(paths, 1, "Output", self._output, self._choose_output)
        self._add_path_row(
            paths,
            2,
            "FFmpeg bin",
            self._ffmpeg_directory,
            self._choose_ffmpeg,
            optional=True,
        )

        options = ttk.LabelFrame(outer, text="Conversion options", padding=10)
        options.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        options.columnconfigure(1, weight=1)
        self._add_combo(options, 0, "Grouping", self._grouping, _GROUPING_LABELS)
        self._add_combo(options, 1, "Output layout", self._layout, _LAYOUT_LABELS)
        self._add_combo(options, 2, "Video format", self._format, _FORMAT_LABELS)
        self._add_combo(options, 3, "File naming", self._naming, _NAMING_LABELS)

        middle = ttk.Panedwindow(outer, orient=tk.VERTICAL)
        middle.grid(row=4, column=0, sticky="nsew", pady=(10, 0))

        preview_frame = ttk.LabelFrame(middle, text="Scan results", padding=8)
        preview_frame.rowconfigure(1, weight=1)
        preview_frame.columnconfigure(0, weight=1)
        scan_status = ttk.Frame(preview_frame)
        scan_status.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        scan_status.columnconfigure(0, weight=1)
        ttk.Label(scan_status, textvariable=self._scan_detail).grid(
            row=0, column=0, sticky="w"
        )
        self._scan_progress = ttk.Progressbar(scan_status, mode="indeterminate")
        self._scan_progress.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        self._groups_table = ttk.Treeview(
            preview_frame,
            columns=("date", "category", "clips"),
            show="headings",
            height=6,
        )
        self._groups_table.heading("date", text="Date / folder")
        self._groups_table.heading("category", text="Category")
        self._groups_table.heading("clips", text="Clips")
        self._groups_table.column("date", width=260)
        self._groups_table.column("category", width=260)
        self._groups_table.column("clips", width=70, anchor=tk.E)
        self._groups_table.grid(row=1, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(
            preview_frame, orient=tk.VERTICAL, command=self._groups_table.yview
        )
        preview_scroll.grid(row=1, column=1, sticky="ns")
        self._groups_table.configure(yscrollcommand=preview_scroll.set)
        middle.add(preview_frame, weight=1)

        progress_frame = ttk.LabelFrame(middle, text="Conversion progress", padding=8)
        progress_frame.columnconfigure(0, weight=1)
        ttk.Label(progress_frame, textvariable=self._overall_detail).grid(
            row=0, column=0, sticky="w"
        )
        self._overall_progress = ttk.Progressbar(
            progress_frame, mode="determinate", maximum=1
        )
        self._overall_progress.grid(row=1, column=0, sticky="ew", pady=(4, 7))
        ttk.Label(progress_frame, textvariable=self._current_detail).grid(
            row=2, column=0, sticky="w"
        )
        self._current_progress = ttk.Progressbar(
            progress_frame, mode="determinate", maximum=100
        )
        self._current_progress.grid(row=3, column=0, sticky="ew", pady=(4, 7))
        self._log = tk.Text(progress_frame, height=7, wrap="word", state=tk.DISABLED)
        self._log.grid(row=4, column=0, sticky="nsew")
        progress_frame.rowconfigure(4, weight=1)
        log_scroll = ttk.Scrollbar(
            progress_frame, orient=tk.VERTICAL, command=self._log.yview
        )
        log_scroll.grid(row=4, column=1, sticky="ns")
        self._log.configure(yscrollcommand=log_scroll.set)
        middle.add(progress_frame, weight=1)

        controls = ttk.Frame(outer)
        controls.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        controls.columnconfigure(3, weight=1)
        self._scan_button = ttk.Button(
            controls, text="1. Scan source", command=self._start_scan
        )
        self._scan_button.grid(row=0, column=0, padx=(0, 8))
        self._convert_button = ttk.Button(
            controls,
            text="2. Convert videos",
            command=self._start_conversion,
            state=tk.DISABLED,
        )
        self._convert_button.grid(row=0, column=1, padx=(0, 8))
        self._cancel_button = ttk.Button(
            controls, text="Cancel", command=self._cancel, state=tk.DISABLED
        )
        self._cancel_button.grid(row=0, column=2)
        self._mkv_button = ttk.Button(
            controls, text="MKV → MP4 tool", command=self._open_mkv_tool
        )
        self._mkv_button.grid(row=0, column=4, padx=(8, 0))
        self._join_button = ttk.Button(
            controls, text="Join videos", command=self._open_join_tool
        )
        self._join_button.grid(row=0, column=5, padx=(8, 0))
        ttk.Button(
            controls, text="Open output folder", command=self._open_output
        ).grid(row=0, column=6, padx=(8, 0))
        ttk.Button(
            controls, text="Open diagnostic log", command=self._open_diagnostic_log
        ).grid(row=0, column=7, padx=(8, 0))

        status = ttk.Label(
            outer,
            textvariable=self._status,
            relief=tk.SUNKEN,
            anchor="w",
            padding=(6, 4),
        )
        status.grid(row=6, column=0, sticky="ew", pady=(10, 0))

    def _add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Any,
        optional: bool = False,
    ) -> None:
        """Add a labelled folder entry and browse button."""

        label_text = f"{label} (optional)" if optional else label
        ttk.Label(parent, text=label_text).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        ttk.Button(parent, text="Browse…", command=command).grid(
            row=row, column=2, padx=(8, 0), pady=3
        )

    def _add_combo(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        choices: dict[str, Any],
    ) -> None:
        """Add a read-only option selector."""

        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Combobox(
            parent,
            textvariable=variable,
            values=tuple(choices),
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=3)

    def _choose_source(self) -> None:
        """Choose a source folder and visibly acknowledge the action."""

        folder = filedialog.askdirectory(title="Choose a .media source folder")
        if folder:
            self._source.set(folder)
            self._status.set("Source selected. Click Scan source to inspect it.")
            if not self._output.get().strip():
                self._output.set(str(Path(folder).parent / "Converted Videos"))
        else:
            self._status.set("Source selection cancelled; no folder changed.")

    def _choose_output(self) -> None:
        """Choose an output folder and visibly acknowledge the action."""

        folder = filedialog.askdirectory(title="Choose an output folder")
        if folder:
            self._output.set(folder)
            self._status.set("Output folder selected.")
        else:
            self._status.set("Output selection cancelled; no folder changed.")

    def _choose_ffmpeg(self) -> None:
        """Choose the folder containing FFmpeg and FFprobe."""

        folder = filedialog.askdirectory(title="Choose the FFmpeg bin folder")
        if folder:
            self._ffmpeg_directory.set(folder)
            try:
                converter_find_tools(Path(folder))
            except FFmpegNotFoundError as error:
                self._status.set(str(error))
                messagebox.showwarning("FFmpeg not found", str(error))
            else:
                self._status.set("FFmpeg and FFprobe found in the selected folder.")
        else:
            self._status.set("FFmpeg folder selection cancelled; no folder changed.")

    def _open_mkv_tool(self) -> None:
        """Open the modal single-file MKV-to-MP4 converter."""

        if self._busy_operation:
            self._show_input_error(
                f"The application is currently {self._busy_operation}.",
                "Wait for the current operation to finish or cancel it before "
                "opening the MKV to MP4 tool.",
            )
            return
        ffmpeg_text = self._ffmpeg_directory.get().strip()
        MkvToMp4Dialog(
            self._root,
            Path(ffmpeg_text).expanduser() if ffmpeg_text else None,
        )
        self._status.set("MKV to MP4 tool opened.")

    def _open_join_tool(self) -> None:
        """Open the modal folder-wide video joiner."""

        if self._busy_operation:
            self._show_input_error(
                f"The application is currently {self._busy_operation}.",
                "Wait for the current operation to finish or cancel it before "
                "opening the Join videos tool.",
            )
            return
        ffmpeg_text = self._ffmpeg_directory.get().strip()
        JoinMp4Dialog(
            self._root,
            Path(ffmpeg_text).expanduser() if ffmpeg_text else None,
        )
        self._status.set("Join videos tool opened.")

    def _source_or_grouping_changed(self, *_args: object) -> None:
        """Invalidate stale scan data when its inputs change."""

        if self._scan_result is not None and not self._busy_operation:
            self._scan_result = None
            self._convert_button.configure(state=tk.DISABLED)
            self._scan_detail.set("Source or grouping changed — scan again")
            self._clear_group_table()

    def _start_scan(self) -> None:
        """Validate input and launch a responsive source scan."""

        if self._busy_operation:
            self._status.set(f"Already busy with {self._busy_operation}.")
            return
        source = Path(self._source.get().strip()).expanduser()
        if not source.is_dir():
            self._show_input_error(
                "The source folder does not exist or is not currently available.",
                "Reconnect any removable drive, then choose an existing source folder.",
                f"Selected source: {source}",
            )
            return

        grouping = _GROUPING_LABELS[self._grouping.get()]
        self._scan_result = None
        self._clear_group_table()
        self._set_busy("scanning")
        self._scan_detail.set("Scanning folders…")
        self._scan_progress.start(12)
        self._append_log(f"Scanning {source}")

        def worker() -> None:
            try:
                result = scanner_scan(
                    source,
                    grouping,
                    progress=lambda count, path: self._queue_message(
                        ("scan_progress", count, path), lossy=True
                    ),
                    cancel_event=self._cancel_event,
                )
            except ScanCancelled:
                self._queue_message(("scan_cancelled",))
            except Exception as error:  # Surface unexpected filesystem errors.
                # Logged here, at the catch, so queue delivery never duplicates it.
                _diagnostics_exception("Scanning source failed", error)
                self._queue_message(("operation_error", "Scanning source", error))
            else:
                self._queue_message(("scan_done", result))

        threading.Thread(target=worker, name="media-scan", daemon=True).start()

    def _start_conversion(self) -> None:
        """Validate output settings and launch conversion in a worker thread."""

        if self._busy_operation:
            self._status.set(f"Already busy with {self._busy_operation}.")
            return
        if not self._scan_result or not self._scan_result.groups:
            self._show_input_error(
                "There are no scanned .media groups ready to convert.",
                "Choose a source folder and complete Scan source before converting.",
            )
            return
        output_text = self._output.get().strip()
        if not output_text:
            self._show_input_error(
                "No output folder has been selected.",
                "Choose where the completed videos should be saved, then try again.",
            )
            return

        options = ConversionOptions(
            output_root=Path(output_text).expanduser().resolve(),
            output_layout=_LAYOUT_LABELS[self._layout.get()],
            video_format=_FORMAT_LABELS[self._format.get()],
            naming_mode=_NAMING_LABELS[self._naming.get()],
            ffmpeg_directory=(
                Path(self._ffmpeg_directory.get().strip()).expanduser()
                if self._ffmpeg_directory.get().strip()
                else None
            ),
        )
        groups = self._scan_result.groups
        self._set_busy("converting")
        self._overall_progress.configure(maximum=max(1, len(groups)), value=0)
        self._overall_detail.set(f"Preparing 0 of {len(groups)} videos")
        self._current_progress.configure(mode="determinate", maximum=100, value=0)
        self._current_detail.set("Locating FFmpeg…")
        self._append_log(f"Starting conversion of {len(groups)} video group(s).")

        def worker() -> None:
            try:
                summary = converter_convert(
                    groups,
                    options,
                    event=lambda name, details: self._queue_message(
                        ("conversion_event", name, details),
                        lossy=name in {"validation_progress", "encoding_progress"},
                    ),
                    cancel_event=self._cancel_event,
                )
            except Exception as error:  # Includes missing dependencies.
                # Logged here, at the catch, so queue delivery never duplicates it.
                _diagnostics_exception("Converting videos failed", error)
                self._queue_message(("operation_error", "Converting videos", error))
            else:
                self._queue_message(("conversion_done", summary))

        threading.Thread(target=worker, name="media-convert", daemon=True).start()

    def _handle_conversion_event(self, name: str, details: dict[str, object]) -> None:
        """Render one structured converter progress event."""

        if name == "group_started":
            index = int(details["index"])
            total = int(details["total"])
            target = Path(details["target"])
            self._overall_detail.set(f"Video {index} of {total}: {target.name}")
            self._current_detail.set(
                f"Validating 0 of {details['file_count']} clips for {target.name}"
            )
            self._current_progress.stop()
            self._current_progress.configure(mode="determinate", value=0)
            self._append_log(f"Validating clips for {target}")
        elif name == "validation_progress":
            current = int(details["current"])
            total = max(1, int(details["total"]))
            self._current_progress.configure(
                mode="determinate", maximum=total, value=current
            )
            self._current_detail.set(f"Validated {current} of {total} clips")
        elif name == "file_skipped":
            self._append_log(
                "Skipped unreadable clip; conversion will continue with the remaining "
                f"clips: {details['path']}"
            )
        elif name == "encoding_progress":
            fraction = details.get("fraction")
            if fraction is None:
                if str(self._current_progress.cget("mode")) != "indeterminate":
                    self._current_progress.configure(mode="indeterminate")
                    self._current_progress.start(12)
                self._current_detail.set("Encoding video… duration unavailable")
            else:
                self._current_progress.stop()
                self._current_progress.configure(
                    mode="determinate", maximum=100, value=float(fraction) * 100
                )
                self._current_detail.set(f"Encoding video… {float(fraction) * 100:.1f}%")
        elif name == "group_completed":
            index = int(details["index"])
            self._overall_progress.configure(value=index)
            self._current_progress.stop()
            self._current_progress.configure(mode="determinate", maximum=100, value=100)
            self._append_log(f"Completed: {details['target']}")
        elif name == "group_failed":
            self._append_log(f"Failed: {details['reason']}")

    def _finish_scan(self, result: ScanResult) -> None:
        """Display scan counts and group preview."""

        self._scan_progress.stop()
        self._scan_result = result
        self._set_idle()
        self._scan_detail.set(
            f"Found {result.media_file_count} clips across {result.day_count} day(s), "
            f"creating {len(result.groups)} video(s)."
        )
        for group in result.groups:
            date_label = (
                f"{group.year}/{group.month}/{group.day}"
                if group.year
                else str(group.day_root)
            )
            self._groups_table.insert(
                "", tk.END, values=(date_label, group.category or "All clips", len(group.files))
            )
        if result.media_file_count:
            self._convert_button.configure(state=tk.NORMAL)
            self._status.set(
                f"Scan complete: {len(result.groups)} output video(s) ready to convert."
            )
            if result.unrecognised_date_files:
                self._append_log(
                    f"Note: {result.unrecognised_date_files} clip(s) were outside a "
                    "YYYY/MM/DD path and were grouped under the selected source."
                )
        else:
            self._status.set("Scan complete: no .media files were found.")
            messagebox.showwarning(
                "No .media files found",
                error_messages_format(
                    "Scanning source",
                    "No files ending in .media were found below the selected folder.",
                    "Confirm the correct camera folder is selected and that the drive "
                    "is fully connected, then scan again.",
                    f"Scanned folder: {result.source_root}",
                ),
            )

    def _finish_conversion(self, summary: ConversionSummary) -> None:
        """Display a complete, cancelled, or partially failed run summary."""

        self._current_progress.stop()
        self._set_idle()
        if summary.cancelled:
            self._status.set(
                f"Conversion cancelled after {len(summary.completed)} completed video(s)."
            )
            self._current_detail.set("Cancelled")
            self._append_log("Conversion cancelled by user.")
            return

        completed = len(summary.completed)
        failed = len(summary.failed_groups)
        skipped = len(summary.skipped_files)
        self._overall_progress.configure(value=completed + failed)
        self._overall_detail.set(f"Finished: {completed} completed, {failed} failed")
        self._current_detail.set("Conversion run finished")
        self._status.set(
            f"Finished: {completed} video(s) created, {failed} failed, "
            f"{skipped} unreadable clip(s) skipped."
        )
        detail = self._status.get()
        if summary.failed_groups:
            detail += "\n\nFailure details:\n\n" + "\n\n".join(
                summary.failed_groups[:8]
            )
            messagebox.showwarning("Conversion finished with errors", detail)
        else:
            messagebox.showinfo("Conversion finished", detail)

    def _poll_messages(self) -> None:
        """Process worker messages on Tk's main thread."""

        try:
            while True:
                message = self._messages.get_nowait()
                kind = message[0]
                if kind == "scan_progress":
                    self._scan_detail.set(
                        f"Scanning… checked {message[1]} items\n{message[2]}"
                    )
                elif kind == "scan_done":
                    self._finish_scan(message[1])
                elif kind == "scan_cancelled":
                    self._scan_progress.stop()
                    self._scan_detail.set("Scan cancelled")
                    self._status.set("Scan cancelled; no results were changed.")
                    self._set_idle()
                elif kind == "conversion_event":
                    self._handle_conversion_event(message[1], message[2])
                elif kind == "conversion_done":
                    self._finish_conversion(message[1])
                elif kind == "operation_error":
                    self._scan_progress.stop()
                    self._current_progress.stop()
                    self._set_idle()
                    if isinstance(message[2], FFmpegNotFoundError):
                        details = str(message[2])
                    else:
                        details = error_messages_format_operation(message[1], message[2])
                    self._status.set(f"{message[1]} could not complete. See the error dialog.")
                    self._append_log(details)
                    messagebox.showerror(f"{message[1]} error", details)
        except queue.Empty:
            pass
        finally:
            self._root.after(100, self._poll_messages)

    def _set_busy(self, operation: str) -> None:
        """Disable conflicting actions and enable cancellation."""

        self._busy_operation = operation
        self._cancel_event.clear()
        self._scan_button.configure(state=tk.DISABLED)
        self._convert_button.configure(state=tk.DISABLED)
        self._mkv_button.configure(state=tk.DISABLED)
        self._join_button.configure(state=tk.DISABLED)
        self._cancel_button.configure(state=tk.NORMAL)
        self._status.set(f"{operation.capitalize()} in progress…")

    def _set_idle(self) -> None:
        """Restore controls after a worker finishes."""

        self._busy_operation = None
        self._scan_button.configure(state=tk.NORMAL)
        self._mkv_button.configure(state=tk.NORMAL)
        self._join_button.configure(state=tk.NORMAL)
        self._cancel_button.configure(state=tk.DISABLED)
        if self._scan_result and self._scan_result.groups:
            self._convert_button.configure(state=tk.NORMAL)

    def _cancel(self) -> None:
        """Request cancellation and immediately acknowledge the click."""

        if not self._busy_operation:
            self._status.set("Nothing is currently running to cancel.")
            return
        self._cancel_event.set()
        self._cancel_button.configure(state=tk.DISABLED)
        self._status.set(
            f"Cancellation requested; waiting for {self._busy_operation} to stop safely…"
        )

    def _open_output(self) -> None:
        """Open the output directory using the host operating system."""

        output_text = self._output.get().strip()
        if not output_text:
            self._show_input_error(
                "No output folder has been selected.",
                "Choose an output folder before using Open output folder.",
            )
            return
        output = Path(output_text).expanduser()
        if not output.is_dir():
            self._show_input_error(
                "The output folder does not exist yet.",
                "Run a conversion first or choose an existing output folder.",
                f"Selected output: {output}",
            )
            return
        self._status.set(f"Opening output folder: {output}")
        try:
            if os.name == "nt":
                os.startfile(output)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(output)])
            else:
                subprocess.Popen(["xdg-open", str(output)])
        except OSError as error:
            details = error_messages_format_operation("Opening output folder", error)
            self._status.set("The output folder could not be opened. See the error dialog.")
            self._append_log(details)
            messagebox.showerror("Open output folder error", details)

    def _show_input_error(
        self, problem: str, action: str, technical: str | None = None
    ) -> None:
        """Show validation feedback in both status bar and dialog."""

        details = error_messages_format("Checking settings", problem, action, technical)
        self._status.set(problem)
        self._append_log(details)
        messagebox.showwarning("Action needed", details)

    def _queue_message(self, message: tuple[Any, ...], lossy: bool = False) -> None:
        """Queue worker messages without allowing unbounded memory growth."""

        if lossy:
            try:
                self._messages.put_nowait(message)
            except queue.Full:
                pass
            return
        self._messages.put(message)

    def _append_log(self, text: str, mirror: bool = True) -> None:
        """Append one readable line to the activity log and the diagnostic file.

        ``mirror`` is False for a line that is written to the file separately
        with extra structured fields, so it is never recorded twice.
        """

        # Trim oldest lines so long runs do not grow the text buffer forever.
        while self._log_line_count >= _MAX_LOG_LINES:
            self._log.configure(state=tk.NORMAL)
            self._log.delete("1.0", "2.0")
            self._log.configure(state=tk.DISABLED)
            self._log_line_count -= 1

        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text.rstrip() + "\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)
        self._log_line_count += 1
        # Mirror the same line to the persistent log the user can open.
        if mirror:
            _diagnostics_info(text)

    @property
    def ready_announced(self) -> bool:
        """True once the window really mapped and the ready line was logged."""

        return self._ready_announced

    def _announce_ready(self, _event: object = None) -> None:
        """Log the canonical running line after the window is really drawn."""

        if self._ready_announced or not self._interface_is_drawn():
            return
        self._ready_announced = True
        self._status.set(
            "Application should be running now. Choose a source folder; open diagnostic log for help."
        )
        path = _diagnostics_log_path()
        recovery = _diagnostics_recovery_text()
        self._append_log(_READY_MESSAGE, mirror=False)
        self._append_log(
            f"Diagnostic log: {path}" if path else "Diagnostic log unavailable."
        )
        if recovery:
            self._append_log(recovery)
        _diagnostics_info(
            _READY_MESSAGE,
            log=str(path) if path else None,
            tk=self._tk_patchlevel(),
            python=sys.version.split()[0],
        )

    def _tk_patchlevel(self) -> str:
        """Return the Tk version reported by the real window.

        The runtime check and the bundle self-test ask Tk the same way, so the
        ready record reports the same value they validate.
        """

        try:
            return str(self._root.tk.call("package", "require", "Tk"))
        except Exception:  # noqa: BLE001 - report the fallback rather than fail
            return str(getattr(tk, "TkVersion", "unknown"))

    def _interface_is_drawn(self) -> bool:
        """True once the real window and its main controls are on screen."""

        try:
            return bool(self._root.winfo_viewable()) and self._scan_button.winfo_width() > 1
        except tk.TclError:
            return False

    def _report_callback_exception(
        self, exc_type: Any, exc_value: Any, _exc_traceback: Any
    ) -> None:
        """Log and show a failed Tk callback instead of losing it in a console."""

        error = exc_value if isinstance(exc_value, BaseException) else exc_type
        _diagnostics_exception("Interface callback failed", error)
        details = error_messages_format_operation("Running the interface", error)
        try:
            self._append_log(details)
            messagebox.showerror("Application error", details)
        except tk.TclError:
            return

    def _open_diagnostic_log(self) -> None:
        """Open the diagnostic log without touching any running conversion."""

        try:
            opened = diagnostics.diagnostics_open_log()
        except Exception as error:  # noqa: BLE001 - the control must stay safe
            _diagnostics_exception("Opening the diagnostic log failed", error)
            opened = False
        if opened:
            path = _diagnostics_log_path()
            self._status.set(f"Opened diagnostic log: {path or 'this session'}")
            self._append_log(f"Opened diagnostic log: {path or 'this session'}")
            return
        recovery = _diagnostics_recovery_text()
        self._status.set(f"The diagnostic log is unavailable. {recovery}")
        self._append_log(f"Diagnostic log unavailable. {recovery}")
        messagebox.showwarning("Diagnostic log", recovery)

    def _clear_group_table(self) -> None:
        """Remove all stale rows from the scan preview."""

        for item in self._groups_table.get_children():
            self._groups_table.delete(item)

    def _on_close(self) -> None:
        """Protect an active output from an accidental window close."""

        if self._busy_operation and not messagebox.askyesno(
            "Operation running",
            f"{self._busy_operation.capitalize()} is still running. Cancel and exit?",
        ):
            self._status.set(f"Continuing {self._busy_operation}.")
            return
        if self._busy_operation:
            self._cancel_event.set()
        self._append_log(
            f"Application window closing (running operation: {self._busy_operation or 'none'})."
        )
        self._root.destroy()

    def _on_native_quit(self) -> None:
        """Queue the close path instead of running it in Tk's menu stack.

        The macOS App menu Quit command is dispatched from native menu code,
        where ``messagebox`` can hang the interface. Queuing ``_on_close`` keeps
        that command returning immediately while the confirmation still runs.
        """

        self._root.after_idle(self._on_close)


def web_ui_main() -> None:
    """Launch the Mediatovideo Converter desktop application."""

    root = tk.Tk()
    MediaToVideoApp(root)
    root.mainloop()


if __name__ == "__main__":
    # Developer module launches use the same runtime validation as the app.
    from run_app import run_app_main

    raise SystemExit(run_app_main())
