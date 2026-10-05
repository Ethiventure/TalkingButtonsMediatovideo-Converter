"""FFmpeg-backed validation, naming, and conversion services."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Sequence

from .error_messages import error_messages_format, error_messages_path
from .models import (
    ConversionOptions,
    ConversionSummary,
    MediaGroup,
    MkvConversionResult,
    NamingMode,
    OutputLayout,
    VideoFormat,
)

ConversionEvent = Callable[[str, dict[str, object]], None]
# Command runner used by the tool verification helpers. Tests inject a fake so
# version and capability policy can be checked without a real FFmpeg build.
ToolRunner = Callable[[Sequence[str]], tuple[int, str]]
_INVALID_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_VALIDATION_EVENT_INTERVAL_SECONDS = 0.2
_ENCODING_EVENT_INTERVAL_SECONDS = 0.2
_ENCODING_EVENT_MIN_FRACTION_STEP = 0.005

# Video-tool policy. These values live in exactly one module; the build tool
# and the frozen application read them through the getters below. Python and
# Tk requirements belong to the runtime module, not here.
CONVERTER_BUNDLED_TOOLS_DIRNAME = "video_tools"
CONVERTER_MINIMUM_FFMPEG_VERSION = (8, 1, 2)
CONVERTER_REQUIRED_ENCODERS = ("libx264", "aac")
CONVERTER_REQUIRED_DEMUXERS = ("concat",)
_CONVERTER_TOOL_VERSION_PATTERN = re.compile(
    r"^(?P<tool>ffmpeg|ffprobe)\s+version\s+(?P<version>\S+)", re.IGNORECASE
)
_CONVERTER_NUMERIC_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")
_CONVERTER_LISTING_FLAGS_PATTERN = re.compile(r"^[A-Za-z.]{1,10}$")
# Verified reports are cached per binary identity so a manual folder choice
# cannot bypass the minimum versions, yet one conversion does not re-probe the
# tools for every clip group.
_CONVERTER_TOOL_REPORT_CACHE: dict[tuple[object, ...], dict[str, object]] = {}
# LittlelfSmart `.media` frame packing (24-byte header per chunk):
# [u32 type][u32 payload_len][u64 timestamp_ms][u32 unknown][u32 tail=20].
# Type 0/1 carry H.264 video, type 3 carries 16-bit mono PCM at 8000 Hz.
_LITTLELF_HEADER_SIZE = 24
_LITTLELF_VIDEO_TYPES = (0, 1)
_LITTLELF_AUDIO_TYPE = 3
_LITTLELF_AUDIO_RATE = 8000
_LITTLELF_AUDIO_CHANNELS = 1


class ConversionCancelled(Exception):
    """Raised internally when conversion cancellation is requested."""


class FFmpegNotFoundError(RuntimeError):
    """Raised when FFmpeg and FFprobe cannot be located."""


class FFmpegCompatibilityError(RuntimeError):
    """Raised when FFmpeg or FFprobe is too old or lacks a required feature."""


class ConversionProcessError(RuntimeError):
    """Raised with a complete user-facing explanation of a group failure."""


def _converter_version_text(version: Sequence[int] | None) -> str:
    """Render a version tuple such as ``(8, 1, 2)`` as ``"8.1.2"``."""

    return ".".join(str(part) for part in version) if version else "unknown"


def converter_bundled_tools_dirname() -> str:
    """Return the in-bundle folder that holds the packaged video tools."""

    return CONVERTER_BUNDLED_TOOLS_DIRNAME


def converter_minimum_versions() -> dict[str, str]:
    """Return the supported minimum FFmpeg and FFprobe versions.

    Python and Tk policy is deliberately absent: the runtime module owns it.
    """

    minimum = _converter_version_text(CONVERTER_MINIMUM_FFMPEG_VERSION)
    return {"ffmpeg": minimum, "ffprobe": minimum}


def _converter_frozen_root() -> Path | None:
    """Return the PyInstaller extraction root while running frozen, else None."""

    if not getattr(sys, "frozen", False):
        return None
    extraction_root = getattr(sys, "_MEIPASS", None)
    return Path(extraction_root) if extraction_root else None


def converter_find_tools(ffmpeg_directory: Path | None = None) -> tuple[str, str]:
    """Locate FFmpeg and FFprobe using the packaged-application policy.

    Resolution order is fixed in this module so every entry point agrees:

    1. An explicitly selected folder (the user override in the interface).
    2. The packaged ``video_tools`` folder inside a frozen application.
    3. ``PATH``, which is only used when running from a source checkout.

    A frozen application deliberately never falls back to ``PATH``. A missing
    or empty bundled pair is reported as an incomplete installation so the app
    cannot silently run a different FFmpeg build than the one it shipped with.
    """

    executable_suffix = ".exe" if os.name == "nt" else ""
    if ffmpeg_directory:
        directory = ffmpeg_directory.expanduser().resolve()
        ffmpeg, ffprobe = _converter_require_tool_pair(directory, executable_suffix)
        if ffmpeg and ffprobe:
            return ffmpeg, ffprobe
        raise FFmpegNotFoundError(
            error_messages_format(
                "Checking video tools",
                "FFmpeg and FFprobe were not both found in the selected folder.",
                "Select the folder containing both executables, or install the "
                "application again so its packaged video tools are restored.",
                error_messages_path(directory),
            )
        )

    frozen_root = _converter_frozen_root()
    if getattr(sys, "frozen", False):
        if frozen_root is None:
            raise FFmpegNotFoundError(error_messages_format(
                "Checking packaged video tools", "The application bundle root is unavailable.",
                "Install the application again from a fresh download."
            ))
        directory = frozen_root / CONVERTER_BUNDLED_TOOLS_DIRNAME
        ffmpeg, ffprobe = _converter_require_tool_pair(
            directory, executable_suffix, require_nonempty=True
        )
        if ffmpeg and ffprobe:
            return ffmpeg, ffprobe
        raise FFmpegNotFoundError(
            error_messages_format(
                "Checking packaged video tools",
                "The packaged FFmpeg and FFprobe tools are missing or incomplete.",
                "Install the application again from a fresh download, or select a "
                "folder containing both executables in the app.",
                error_messages_path(directory),
            )
        )

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        raise FFmpegNotFoundError(
            error_messages_format(
                "Checking video tools",
                "FFmpeg and FFprobe are required but could not both be found.",
                "Install the packaged application, install FFmpeg for this "
                "platform, or select an existing FFmpeg bin folder in the app.",
            )
        )
    return ffmpeg_path, ffprobe_path


def converter_verify_tools(
    ffmpeg: str,
    ffprobe: str,
    runner: ToolRunner | None = None,
) -> dict[str, object]:
    """Reject an FFmpeg/FFprobe pair below 8.1.2 or missing a required feature.

    Version strings alone are not enough: the encoders and demuxer the
    conversion pipeline actually uses are checked too, so an old or cut-down
    build is reported before any user media is touched. ``runner`` lets tests
    supply canned tool output.
    """

    required_version = CONVERTER_MINIMUM_FFMPEG_VERSION
    tool_runner = runner or _converter_default_tool_runner
    report: dict[str, object] = {
        "minimum_version": _converter_version_text(required_version),
        "tools": {},
        "features": {},
    }
    for tool_name, tool_path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        version, banner, configuration = _converter_tool_version(
            tool_runner, tool_path, tool_name
        )
        if version is None or version < required_version:
            raise FFmpegCompatibilityError(
                _converter_tool_problem(
                    f"{tool_name} {_converter_version_text(version)} is not supported",
                    f"detected {_converter_version_text(version)}; "
                    f"required {_converter_version_text(required_version)} or newer",
                    f"{tool_name}: {tool_path}\n{banner or 'no version output'}",
                )
            )
        report["tools"][tool_name] = {  # type: ignore[index]
            "version": _converter_version_text(version),
            "banner": banner,
            "configuration": configuration,
        }

    for flag, features in (
        ("-encoders", CONVERTER_REQUIRED_ENCODERS),
        ("-demuxers", CONVERTER_REQUIRED_DEMUXERS),
    ):
        listing = _converter_tool_listing(tool_runner, ffmpeg, flag)
        for feature in features:
            if not _converter_listing_contains(listing, feature):
                raise FFmpegCompatibilityError(
                    _converter_tool_problem(
                        f"This FFmpeg build does not include {feature}",
                        f"missing: {feature}",
                        f"ffmpeg: {ffmpeg}",
                    )
                )
            report["features"][feature] = True  # type: ignore[index]
    return report


def _converter_tool_problem(problem: str, detail: str, technical: str) -> str:
    """Format a video-tool policy failure with the shared stage/action wording."""

    return error_messages_format(
        "Checking video tools",
        f"{problem}.",
        "Install the supported FFmpeg build (the packaged application includes "
        "it) and try again.",
        f"{detail}\n{technical}",
    )


def _converter_verified_tools(ffmpeg: str, ffprobe: str) -> dict[str, object]:
    """Verify the tool pair once per binary identity and cache the report.

    Every conversion path calls this, so a hand-picked or outdated folder
    cannot bypass the minimum versions after the interface has opened.
    """

    key = (ffmpeg, ffprobe) + tuple(
        _converter_file_identity(path) for path in (ffmpeg, ffprobe)
    )
    report = _CONVERTER_TOOL_REPORT_CACHE.get(key)
    if report is None:
        report = converter_verify_tools(ffmpeg, ffprobe)
        _CONVERTER_TOOL_REPORT_CACHE[key] = report
    return report


def _converter_file_identity(path: str) -> tuple[int, int]:
    """Return (size, mtime) so a replaced binary is re-verified."""

    try:
        details = os.stat(path)
    except OSError:
        return (0, 0)
    return (details.st_size, int(details.st_mtime))


def converter_plan_targets(
    groups: Sequence[MediaGroup], options: ConversionOptions
) -> tuple[Path, ...]:
    """Create collision-free target paths without modifying the filesystem."""

    reserved: set[Path] = set()
    targets: list[Path] = []
    extension = ".mkv" if options.video_format is VideoFormat.MKV_COPY else ".mp4"
    for group in groups:
        directory = _converter_output_directory(group, options)
        stem = _converter_output_stem(group, options.naming_mode)
        candidate = directory / f"{stem}{extension}"
        suffix_number = 2
        while candidate in reserved or candidate.exists():
            candidate = directory / f"{stem}-{suffix_number}{extension}"
            suffix_number += 1
        reserved.add(candidate)
        targets.append(candidate)
    return tuple(targets)


def converter_convert(
    groups: Sequence[MediaGroup],
    options: ConversionOptions,
    event: ConversionEvent | None = None,
    cancel_event: threading.Event | None = None,
) -> ConversionSummary:
    """Validate and convert all groups, continuing past individual failures."""

    ffmpeg, ffprobe = converter_find_tools(options.ffmpeg_directory)
    _converter_verified_tools(ffmpeg, ffprobe)
    targets = converter_plan_targets(groups, options)
    completed: list[Path] = []
    failures: list[str] = []
    skipped: list[Path] = []

    try:
        for index, (group, target) in enumerate(zip(groups, targets), start=1):
            _converter_check_cancel(cancel_event)
            _converter_emit(
                event,
                "group_started",
                index=index,
                total=len(groups),
                target=target,
                file_count=len(group.files),
            )
            valid_files: list[Path] = []
            total_duration = 0.0
            last_validation_event_time = 0.0
            for file_index, media_file in enumerate(group.files, start=1):
                _converter_check_cancel(cancel_event)
                duration = _converter_probe_media(ffprobe, media_file)
                if duration is None:
                    skipped.append(media_file)
                    _converter_emit(event, "file_skipped", path=media_file)
                else:
                    valid_files.append(media_file)
                    total_duration += duration
                now = time.monotonic()
                if (
                    file_index == len(group.files)
                    or now - last_validation_event_time
                    >= _VALIDATION_EVENT_INTERVAL_SECONDS
                ):
                    _converter_emit(
                        event,
                        "validation_progress",
                        current=file_index,
                        total=len(group.files),
                        path=media_file,
                    )
                    last_validation_event_time = now

            if not valid_files:
                label = _converter_group_label(group)
                failures.append(
                    f"{label}\n"
                    + error_messages_format(
                        "Validating source clips",
                        "No readable .media clips remained in this group.",
                        "Check that the camera export is complete and try the original "
                        "files again. The other video groups will continue.",
                    )
                )
                _converter_emit(event, "group_failed", target=target, reason=failures[-1])
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _converter_run_ffmpeg(
                    ffmpeg,
                    valid_files,
                    target,
                    options.video_format,
                    total_duration,
                    event,
                    cancel_event,
                )
            except ConversionCancelled:
                raise
            except ConversionProcessError as error:
                reason = str(error)
                failures.append(f"{_converter_group_label(group)}\n{reason}")
                _converter_emit(event, "group_failed", target=target, reason=reason)
                continue
            except OSError as error:
                reason = _converter_explain_output_error(error, target)
                failures.append(f"{_converter_group_label(group)}\n{reason}")
                _converter_emit(event, "group_failed", target=target, reason=reason)
                continue

            completed.append(target)
            _converter_emit(
                event,
                "group_completed",
                index=index,
                total=len(groups),
                target=target,
            )
    except ConversionCancelled:
        return ConversionSummary(
            completed=tuple(completed),
            failed_groups=tuple(failures),
            skipped_files=tuple(skipped),
            cancelled=True,
        )

    return ConversionSummary(
        completed=tuple(completed),
        failed_groups=tuple(failures),
        skipped_files=tuple(skipped),
        cancelled=False,
    )


def converter_convert_mkv_to_mp4(
    source: Path,
    target: Path,
    ffmpeg_directory: Path | None = None,
    event: ConversionEvent | None = None,
    cancel_event: threading.Event | None = None,
) -> MkvConversionResult:
    """Convert one MKV to a compatible H.264/AAC MP4 without overwriting files."""

    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_file():
        raise ConversionProcessError(
            error_messages_format(
                "Checking MKV input",
                "The selected MKV file does not exist or is not currently available.",
                "Reconnect the source drive or select an existing MKV file, then try again.",
                error_messages_path(source),
            )
        )
    if source.suffix.casefold() != ".mkv":
        raise ConversionProcessError(
            error_messages_format(
                "Checking MKV input",
                "The selected input is not an MKV file.",
                "Select a file whose name ends in .mkv.",
                error_messages_path(source),
            )
        )
    if target.suffix.casefold() != ".mp4":
        raise ConversionProcessError(
            error_messages_format(
                "Checking MP4 output",
                "The output filename does not end in .mp4.",
                "Choose an output filename ending in .mp4.",
                error_messages_path(target),
            )
        )
    if target.exists():
        raise ConversionProcessError(
            error_messages_format(
                "Checking MP4 output",
                "The selected output file already exists and will not be overwritten.",
                "Choose a different MP4 filename or move the existing file, then try again.",
                error_messages_path(target),
            )
        )

    ffmpeg, ffprobe = converter_find_tools(ffmpeg_directory)
    _converter_verified_tools(ffmpeg, ffprobe)
    _converter_check_cancel(cancel_event)
    duration = _converter_probe_media(ffprobe, source)
    if duration is None:
        raise ConversionProcessError(
            error_messages_format(
                "Reading MKV input",
                "FFprobe could not read the selected MKV file.",
                "Confirm the file plays correctly and copy it from the source drive again "
                "if it may be incomplete.",
                error_messages_path(source),
            )
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConversionProcessError(
            _converter_explain_output_error(error, target)
        ) from error

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
    ]
    _converter_emit(
        event,
        "single_started",
        source=source,
        target=target,
        duration=duration,
    )
    try:
        _converter_execute_ffmpeg(
            command,
            target,
            VideoFormat.MP4_H264,
            duration,
            event,
            cancel_event,
        )
    except ConversionCancelled:
        return MkvConversionResult(output=None, cancelled=True)
    _converter_emit(event, "single_completed", source=source, target=target)
    return MkvConversionResult(output=target, cancelled=False)


_EPOCH_RE = re.compile(r"(\d{10})")
_DATE_STEM_RE = re.compile(r"(\d{2})-(\d{2})")
_JOIN_YEAR_RE = re.compile(r"^(\d{4})\b")
_JOIN_MONTH_DAY_RE = re.compile(r"^\d{1,2}$")


def converter_collect_mp4s(
    folder: Path, extensions: Sequence[str] = (".mp4",)
) -> tuple[Path, ...]:
    """Find video pieces below ``folder`` in stable path order.

    Subfolders are included. MP4 is the default; the join dialog also
    accepts MKV pieces, which this app writes with the same tracks.
    """

    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise ConversionProcessError(
            error_messages_format(
                "Checking video folder",
                "The selected folder does not exist or is not currently available.",
                "Reconnect any removable drive, then choose an existing folder.",
                error_messages_path(folder),
            )
        )
    wanted = {extension.casefold() for extension in extensions}
    found = sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.casefold() in wanted
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    return tuple(found)


def converter_order_folder_key(path: Path) -> str:
    """Original order: stable full-path order, as the scanner discovers files."""

    return path.as_posix().casefold()


def converter_order_creation_key(path: Path) -> float:
    """Creation order: file modification time, oldest first.

    The converter writes pieces one group at a time in event order, so
    creation order recovers time order even when filenames carry no event
    time (e.g. Month-Day naming with collision numbers). Do not move or
    copy the pieces first: that rewrites the times.
    """

    return path.stat().st_mtime


def converter_order_time_key(path: Path) -> tuple[str, str, str, int, str]:
    """Time order: (year, month, day, epoch, filename) parsed from path and name.

    Year prefers a YYYY folder above the file, else empty (single-year runs
    sort correctly). Month/day prefer an MM-DD filename stem, else mirror
    folders. Epoch prefers the 10-digit event time embedded by the app's
    Month-Day-Category names, else 0 so such files sort before timed ones.
    """

    name = path.name
    year = ""
    for part in path.parts:
        year_match = _JOIN_YEAR_RE.match(part)
        if year_match:
            year = year_match.group(1)
    month, day = "", ""
    stem_match = _DATE_STEM_RE.match(name)
    if stem_match:
        month, day = stem_match.group(1), stem_match.group(2)
    else:
        parts = path.parts
        for index in range(len(parts) - 2):
            if (
                _JOIN_MONTH_DAY_RE.fullmatch(parts[index])
                and _JOIN_MONTH_DAY_RE.fullmatch(parts[index + 1])
                and 1 <= int(parts[index]) <= 12
                and 1 <= int(parts[index + 1]) <= 31
            ):
                month, day = f"{int(parts[index]):02d}", f"{int(parts[index + 1]):02d}"
    epoch_match = _EPOCH_RE.search(name)
    epoch = int(epoch_match.group(1)) if epoch_match else 0
    return (year, month, day, epoch, name.casefold())


def converter_join_mp4s(
    sources: Sequence[Path],
    target: Path,
    ffmpeg_directory: Path | None = None,
    event: ConversionEvent | None = None,
    cancel_event: threading.Event | None = None,
) -> MkvConversionResult:
    """Join MP4/MKV pieces into one video with fast stream copy, in order."""

    ordered = [Path(source).expanduser().resolve() for source in sources]
    target = target.expanduser().resolve()
    joinable = {".mp4", ".mkv"}
    if len(ordered) < 2:
        raise ConversionProcessError(
            error_messages_format(
                "Checking video inputs",
                "Joining needs at least two video files.",
                "Choose the folder containing the converted pieces, then try again.",
            )
        )
    for source in ordered:
        if not source.is_file():
            raise ConversionProcessError(
                error_messages_format(
                    "Checking MP4 inputs",
                    "One of the MP4 files is missing.",
                    "Run the piece conversion again or remove the missing file from the list.",
                    error_messages_path(source),
                )
            )
        if source.suffix.casefold() not in joinable:
            raise ConversionProcessError(
                error_messages_format(
                    "Checking video inputs",
                    "One of the selected inputs is not a joinable video file.",
                    "Join only MP4 or MKV pieces made by this app.",
                    error_messages_path(source),
                )
            )
    if target.suffix.casefold() not in joinable:
        raise ConversionProcessError(
            error_messages_format(
                "Checking join output",
                "The output filename must end in .mp4 or .mkv.",
                "Choose an output filename ending in .mp4 or .mkv.",
                error_messages_path(target),
            )
        )
    if target.exists():
        raise ConversionProcessError(
            error_messages_format(
                "Checking MP4 output",
                "The selected output file already exists and will not be overwritten.",
                "Choose a different MP4 filename or move the existing file, then try again.",
                error_messages_path(target),
            )
        )

    ffmpeg, ffprobe = converter_find_tools(ffmpeg_directory)
    _converter_check_cancel(cancel_event)
    audio_layouts: list[bool] = []
    total_duration = 0.0
    for source in ordered:
        _converter_check_cancel(cancel_event)
        streams = _converter_probe_streams(ffprobe, source)
        if streams is None:
            raise ConversionProcessError(
                error_messages_format(
                    "Reading MP4 inputs",
                    "FFprobe could not read one of the MP4 files.",
                    "Confirm the file plays correctly and re-convert it if it may be incomplete.",
                    error_messages_path(source),
                )
            )
        video_codecs, audio_codecs, duration = streams
        if video_codecs != ["h264"]:
            raise ConversionProcessError(
                error_messages_format(
                    "Checking MP4 inputs",
                    "One file does not hold the expected H.264 picture track.",
                    "Join only pieces made by this app, or re-convert the odd file first.",
                    f"{error_messages_path(source)}\nVideo tracks: {', '.join(video_codecs) or 'none'}",
                )
            )
        if audio_codecs and audio_codecs != ["aac"]:
            raise ConversionProcessError(
                error_messages_format(
                    "Checking MP4 inputs",
                    "One file holds a sound track this join cannot copy.",
                    "Join only pieces made by this app, or re-convert the odd file first.",
                    f"{error_messages_path(source)}\nSound tracks: {', '.join(audio_codecs)}",
                )
            )
        audio_layouts.append(bool(audio_codecs))
        total_duration += duration
    if any(audio_layouts) and not all(audio_layouts):
        offenders = ", ".join(
            source.name for source, has in zip(ordered, audio_layouts) if not has
        )
        raise ConversionProcessError(
            error_messages_format(
                "Checking MP4 inputs",
                "Some pieces have sound and some are silent, so they cannot share one join.",
                "Join the sounding pieces and the silent pieces as two separate videos.",
                f"Silent files: {offenders}",
            )
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConversionProcessError(
            _converter_explain_output_error(error, target)
        ) from error

    with tempfile.TemporaryDirectory(prefix="mediatovideo-join-") as temporary:
        concat_path = Path(temporary) / "pieces.ffconcat"
        concat_path.write_text(
            "ffconcat version 1.0\n"
            + "".join(
                f"file '{_converter_escape_concat_path(path)}'\n" for path in ordered
            ),
            encoding="utf-8",
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
        ]
        _converter_emit(
            event,
            "single_started",
            sources=[str(source) for source in ordered],
            target=target,
            duration=total_duration,
        )
        try:
            _converter_execute_ffmpeg(
                command,
                target,
                VideoFormat.MP4_H264,
                total_duration,
                event,
                cancel_event,
            )
        except ConversionCancelled:
            return MkvConversionResult(output=None, cancelled=True)
    _converter_emit(event, "single_completed", sources=ordered, target=target)
    return MkvConversionResult(output=target, cancelled=False)


def _converter_probe_streams(
    ffprobe: str, media_file: Path
) -> tuple[list[str], list[str], float] | None:
    """Return (video codecs, audio codecs, duration) or None when unreadable."""

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,codec_type:format=duration",
        "-of",
        "default=noprint_wrappers=1",
        str(media_file),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            **_converter_subprocess_window_options(),
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    video_codecs: list[str] = []
    audio_codecs: list[str] = []
    duration = 0.0
    pending_name: str | None = None
    pending_type: str | None = None

    def flush_stream() -> None:
        nonlocal pending_name, pending_type
        if pending_name and pending_type == "video":
            video_codecs.append(pending_name)
        elif pending_name and pending_type == "audio":
            audio_codecs.append(pending_name)
        pending_name, pending_type = None, None

    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key == "codec_name":
            if pending_name is not None:
                flush_stream()
            pending_name = value
            if pending_type is not None:
                flush_stream()
        elif key == "codec_type":
            if pending_type is not None:
                flush_stream()
            pending_type = value
            if pending_name is not None:
                flush_stream()
        elif key == "duration":
            try:
                duration = max(0.0, float(value))
            except ValueError:
                duration = 0.0
    flush_stream()
    if not video_codecs:
        return None
    return video_codecs, audio_codecs, duration


def _converter_probe_media(ffprobe: str, media_file: Path) -> float | None:
    """Return duration for a readable clip, using zero when duration is unknown."""

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_file),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            **_converter_subprocess_window_options(),
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return 0.0


def _converter_try_demux_littlelf(data: bytes) -> tuple[bytes, bytes] | None:
    """Split LittlelfSmart `.media` bytes into (video, audio).

    Returns None when the bytes do not follow the 24-byte-header packing,
    so plain H.264 files fall back to the previous direct-concat path.
    The trailing flag word varies per camera (20, 24, 81, ...) and is not
    validated; only the 24-byte size, sane lengths, and types 0/1/3 gate.
    """

    video = bytearray()
    audio = bytearray()
    offset = 0
    total = len(data)
    while offset + _LITTLELF_HEADER_SIZE <= total:
        frame_type, payload_len = struct.unpack_from("<II", data, offset)
        if payload_len > 20_000_000:
            return None
        if offset + _LITTLELF_HEADER_SIZE + payload_len > total:
            return None
        payload = data[
            offset + _LITTLELF_HEADER_SIZE : offset
            + _LITTLELF_HEADER_SIZE
            + payload_len
        ]
        if frame_type in _LITTLELF_VIDEO_TYPES:
            video.extend(payload)
        elif frame_type == _LITTLELF_AUDIO_TYPE:
            audio.extend(payload)
        else:
            return None
        offset += _LITTLELF_HEADER_SIZE + payload_len
    if offset != total or not video:
        return None
    return bytes(video), bytes(audio)


def _converter_demux_littlelf_file(
    data: bytes,
) -> tuple[bytes, bytes, float, int] | None:
    """Split one file into (video, audio, fps, video_frame_count).

    None when the bytes are not Littlelf-packed. The frame count pairs with
    ``fps`` to give the exact picture length, so the segment can be cut to a
    fixed duration instead of trusting stream timestamps.
    """

    video = bytearray()
    audio = bytearray()
    video_ts: list[int] = []
    offset = 0
    total = len(data)
    while offset + _LITTLELF_HEADER_SIZE <= total:
        frame_type, payload_len = struct.unpack_from("<II", data, offset)
        timestamp = struct.unpack_from("<Q", data, offset + 8)[0]
        if payload_len > 20_000_000:
            return None
        if offset + _LITTLELF_HEADER_SIZE + payload_len > total:
            return None
        payload = data[
            offset + _LITTLELF_HEADER_SIZE : offset
            + _LITTLELF_HEADER_SIZE
            + payload_len
        ]
        if frame_type in _LITTLELF_VIDEO_TYPES:
            video.extend(payload)
            video_ts.append(timestamp)
        elif frame_type == _LITTLELF_AUDIO_TYPE:
            audio.extend(payload)
        else:
            return None
        offset += _LITTLELF_HEADER_SIZE + payload_len
    if offset != total or not video:
        return None
    fps = 20.0
    if len(video_ts) >= 2:
        span = (video_ts[-1] - video_ts[0]) / 1000.0
        if span > 0.2:
            fps = min(30.0, max(10.0, len(video_ts) / span))
    return bytes(video), bytes(audio), fps, len(video_ts)


def _converter_segment_cut(video_frames: int, fps: float, audio_bytes: int) -> float:
    """Exact segment length: the shorter of picture and sound, in seconds."""

    picture = video_frames / fps if fps > 0 else 0.0
    sound = audio_bytes / (_LITTLELF_AUDIO_RATE * _LITTLELF_AUDIO_CHANNELS * 2)
    if picture <= 0:
        return sound
    if sound <= 0:
        return picture
    return min(picture, sound)


def _converter_run_ffmpeg(
    ffmpeg: str,
    media_files: Sequence[Path],
    target: Path,
    video_format: VideoFormat,
    total_duration: float,
    event: ConversionEvent | None,
    cancel_event: threading.Event | None,
) -> None:
    """Run one FFmpeg concat job and atomically publish its completed output."""

    with tempfile.TemporaryDirectory(prefix="mediatovideo-") as temporary:
        used_littlelf = False
        for probe_file in media_files[:3]:
            try:
                probe_data = Path(probe_file).read_bytes()
            except OSError:
                continue
            if _converter_try_demux_littlelf(probe_data) is not None:
                used_littlelf = True
                break
        if used_littlelf:
            # LittlelfSmart files pack video + PCM sound behind 24-byte
            # headers with per-file clocks (~15-20 fps video, 8 kHz audio).
            # Bulk-joining raw streams drifts, so encode each clip on its own
            # measured fps then concat the segments with copy.
            segment_ext = ".mkv" if video_format is VideoFormat.MKV_COPY else ".mp4"
            segments: list[Path] = []
            for seg_index, media_file in enumerate(media_files):
                _converter_check_cancel(cancel_event)
                data = Path(media_file).read_bytes()
                demuxed = _converter_demux_littlelf_file(data)
                seg_stem = f"seg{seg_index:05d}"
                if demuxed is None:
                    # Plain H.264 fallback: single video input, silent output.
                    seg_video = Path(temporary) / f"{seg_stem}.h264"
                    seg_video.write_bytes(data)
                    seg_target = Path(temporary) / f"{seg_stem}{segment_ext}"
                    seg_command = [
                        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "h264", "-i", str(seg_video),
                        "-map", "0:v:0?",
                    ]
                    if video_format is VideoFormat.MKV_COPY:
                        seg_command.extend(["-c", "copy"])
                    else:
                        seg_command.extend(
                            ["-c:v", "libx264", "-preset", "medium",
                             "-crf", "20", "-movflags", "+faststart"]
                        )
                else:
                    video_part, audio_part, fps, frame_count = demuxed
                    seg_video = Path(temporary) / f"{seg_stem}.h264"
                    seg_video.write_bytes(video_part)
                    seg_command = [
                        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-framerate", f"{fps:.3f}", "-i", str(seg_video),
                    ]
                    if audio_part:
                        seg_audio = Path(temporary) / f"{seg_stem}.raw"
                        seg_audio.write_bytes(audio_part)
                        seg_command.extend(
                            ["-f", "s16le", "-ar", str(_LITTLELF_AUDIO_RATE),
                             "-ac", str(_LITTLELF_AUDIO_CHANNELS),
                             "-i", str(seg_audio)]
                        )
                        cut = _converter_segment_cut(
                            frame_count, fps, len(audio_part)
                        )
                    else:
                        seg_command.extend(
                            ["-f", "lavfi", "-i",
                             f"anullsrc=r={_LITTLELF_AUDIO_RATE}:cl=mono"]
                        )
                        cut = frame_count / fps if fps > 0 else 0.0
                    seg_target = Path(temporary) / f"{seg_stem}{segment_ext}"
                    seg_command.extend(["-map", "0:v:0?", "-map", "1:a:0?"])
                    # Fixed -t cut, never -shortest: copied picture packets
                    # carry no usable timestamps, which starved the sound
                    # track down to zero frames under -shortest.
                    if cut > 0:
                        seg_command.extend(["-t", f"{cut:.3f}"])
                    if video_format is VideoFormat.MKV_COPY:
                        seg_command.extend(
                            ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
                        )
                    else:
                        seg_command.extend(
                            ["-c:v", "libx264", "-preset", "medium",
                             "-crf", "20", "-c:a", "aac", "-b:a", "128k",
                             "-movflags", "+faststart"]
                        )
                _converter_execute_ffmpeg(
                    seg_command, seg_target, video_format,
                    total_duration / max(1, len(media_files)),
                    None, cancel_event,
                )
                try:
                    seg_video.unlink(missing_ok=True)
                except OSError:
                    pass
                raw_candidate = Path(temporary) / f"{seg_stem}.raw"
                try:
                    raw_candidate.unlink(missing_ok=True)
                except OSError:
                    pass
                segments.append(seg_target)
                _converter_emit(
                    event, "encoding_progress",
                    elapsed=float(seg_index + 1),
                    duration=float(len(media_files)),
                    fraction=(seg_index + 1) / max(1, len(media_files)),
                )
            concat_final = Path(temporary) / "segments.ffconcat"
            concat_final.write_text(
                "ffconcat version 1.0\n"
                + "".join(
                    f"file '{_converter_escape_concat_path(p)}'\n" for p in segments
                ),
                encoding="utf-8",
            )
            _converter_execute_ffmpeg(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "concat", "-safe", "0", "-i", str(concat_final),
                 "-c", "copy"],
                target, video_format, total_duration, event, cancel_event,
            )
            return
        concat_path = Path(temporary) / "clips.ffconcat"
        concat_path.write_text(
            "ffconcat version 1.0\n"
            + "".join(
                f"file '{_converter_escape_concat_path(path)}'\n" for path in media_files
            ),
            encoding="utf-8",
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
        ]
        if video_format is VideoFormat.MKV_COPY:
            command.extend(["-c", "copy"])
        else:
            command.extend(
                [
                    "-map",
                    "0:v:0?",
                    "-map",
                    "0:a:0?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                ]
            )
        _converter_execute_ffmpeg(
            command,
            target,
            video_format,
            total_duration,
            event,
            cancel_event,
        )


def _converter_execute_ffmpeg(
    command: list[str],
    target: Path,
    video_format: VideoFormat,
    total_duration: float,
    event: ConversionEvent | None,
    cancel_event: threading.Event | None,
) -> None:
    """Execute one FFmpeg command with progress, cancellation, and atomic output."""

    partial_target = target.with_name(f".{target.stem}.partial{target.suffix}")
    full_command = [
        *command,
        "-progress",
        "pipe:1",
        "-nostats",
        str(partial_target),
    ]
    try:
        process = subprocess.Popen(
            full_command,
            stdout=subprocess.PIPE,
            # Merging stderr prevents a verbose decoder error from filling an
            # unread pipe and deadlocking the progress reader.
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **_converter_subprocess_window_options(),
        )
        assert process.stdout is not None
        diagnostic_lines: deque[str] = deque(maxlen=12)
        last_encoding_event_time = 0.0
        last_encoding_fraction: float | None = None
        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise ConversionCancelled()
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                key, separator, value = line.strip().partition("=")
                if separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        elapsed = int(value) / 1_000_000
                    except ValueError:
                        continue
                    fraction = (
                        min(1.0, elapsed / total_duration) if total_duration else None
                    )
                    now = time.monotonic()
                    should_emit = False
                    if fraction is None:
                        should_emit = (
                            now - last_encoding_event_time
                            >= _ENCODING_EVENT_INTERVAL_SECONDS
                        )
                    else:
                        should_emit = (
                            last_encoding_fraction is None
                            or fraction >= 1.0
                            or fraction - last_encoding_fraction
                            >= _ENCODING_EVENT_MIN_FRACTION_STEP
                            or now - last_encoding_event_time
                            >= _ENCODING_EVENT_INTERVAL_SECONDS
                        )
                    if should_emit:
                        _converter_emit(
                            event,
                            "encoding_progress",
                            elapsed=elapsed,
                            duration=total_duration,
                            fraction=fraction,
                        )
                        last_encoding_event_time = now
                        last_encoding_fraction = fraction
                elif line.strip() and key not in {
                    "bitrate",
                    "drop_frames",
                    "dup_frames",
                    "fps",
                    "frame",
                    "out_time",
                    "progress",
                    "speed",
                    "stream_0_0_q",
                    "total_size",
                }:
                    diagnostic_lines.append(line.strip())
        finally:
            process.stdout.close()

        if process.returncode != 0:
            final_line = diagnostic_lines[-1] if diagnostic_lines else "FFmpeg failed"
            raise ConversionProcessError(
                _converter_explain_ffmpeg_error(final_line, target, video_format)
            )
        if target.exists():
            raise ConversionProcessError(
                error_messages_format(
                    "Saving output video",
                    "The output file appeared during conversion and will not be overwritten.",
                    "Choose a different output filename, then convert again.",
                    error_messages_path(target),
                )
            )
        try:
            os.replace(partial_target, target)
        except OSError as error:
            raise ConversionProcessError(
                _converter_explain_output_error(error, target)
            ) from error
    finally:
        partial_target.unlink(missing_ok=True)


def _converter_output_directory(
    group: MediaGroup, options: ConversionOptions
) -> Path:
    """Apply the output layout policy owned by this module."""

    if (
        options.output_layout is OutputLayout.MIRROR_DATES
        and group.year
        and group.month
        and group.day
    ):
        return options.output_root / group.year / group.month / group.day
    return options.output_root


def _converter_explain_ffmpeg_error(
    diagnostic: str, target: Path, video_format: VideoFormat
) -> str:
    """Translate common FFmpeg diagnostics into an actionable explanation."""

    lower_diagnostic = diagnostic.casefold()
    if "permission denied" in lower_diagnostic:
        problem = "FFmpeg cannot write to the selected output folder."
        action = "Choose an output folder you can write to, then convert again."
    elif "no space left" in lower_diagnostic:
        problem = "The output drive ran out of free space."
        action = "Free disk space or choose another output drive, then convert again."
    elif "unknown encoder" in lower_diagnostic and "libx264" in lower_diagnostic:
        problem = "This FFmpeg installation does not include the H.264 encoder."
        action = (
            "Restart with the platform launcher to install the supported FFmpeg "
            "package, or choose MKV stream-copy output."
        )
    elif any(
        phrase in lower_diagnostic
        for phrase in (
            "invalid data found",
            "could not find codec parameters",
            "error opening input",
        )
    ):
        problem = "One or more camera clips contain unreadable or unsupported data."
        action = (
            "Check the activity log for skipped clips. Try MP4 output if MKV was "
            "selected, or restore the original camera export and retry."
        )
    elif "non-monoton" in lower_diagnostic or "timestamp" in lower_diagnostic:
        problem = "The source clips contain timestamps that cannot be joined as selected."
        action = "Choose MP4 H.264 output so FFmpeg can rebuild the timestamps."
    elif video_format is VideoFormat.MKV_COPY:
        problem = "FFmpeg could not join these clips without changing their streams."
        action = (
            "Try MP4 H.264 output for this source. It is slower but can repair many "
            "stream-compatibility differences."
        )
    else:
        problem = "FFmpeg could not decode or encode this video group."
        action = (
            "Check the source clips are complete and readable, then review the "
            "technical detail below before retrying."
        )
    return error_messages_format(
        "Creating output video",
        problem,
        action,
        f"Output: {target}\nFFmpeg: {diagnostic}",
    )


def _converter_explain_output_error(error: OSError, target: Path) -> str:
    """Explain filesystem failures encountered while creating an output."""

    detail = str(error).strip() or error.__class__.__name__
    lower_detail = detail.casefold()
    if isinstance(error, PermissionError) or "permission denied" in lower_detail:
        problem = "The selected output folder is not writable."
        action = "Choose a writable output folder, then run the conversion again."
    elif "no space left" in lower_detail:
        problem = "The selected output drive does not have enough free space."
        action = "Free disk space or choose another output folder, then try again."
    else:
        problem = "The completed video could not be written to its destination."
        action = (
            "Confirm the output drive is connected and writable, then choose the "
            "output folder again."
        )
    return error_messages_format(
        "Saving output video",
        problem,
        action,
        f"{error_messages_path(target)}\nSystem: {detail}",
    )


def _converter_output_stem(group: MediaGroup, naming_mode: NamingMode) -> str:
    """Create a portable filename stem from group metadata."""

    if group.month and group.day:
        stem = f"{group.month}-{group.day}"
    else:
        stem = group.day_root.name or "video"
    if naming_mode is NamingMode.MONTH_DAY_CATEGORY and group.category:
        stem = f"{stem}-{group.category}"
    sanitised = _INVALID_FILENAME.sub("-", stem).strip(" .-")
    return sanitised or "video"


def _converter_escape_concat_path(path: Path) -> str:
    """Escape a path for FFmpeg's single-quoted concat-file syntax."""

    return path.resolve().as_posix().replace("'", "'\\''")


def _converter_group_label(group: MediaGroup) -> str:
    """Return a concise human-readable group name for diagnostics."""

    date = "-".join(part for part in (group.year, group.month, group.day) if part)
    label = date or group.day_root.name
    return f"{label} / {group.category}" if group.category else label


def _converter_emit(
    event: ConversionEvent | None, event_name: str, **details: object
) -> None:
    """Safely emit a structured progress event when a listener exists."""

    if event:
        event(event_name, details)


def _converter_check_cancel(cancel_event: threading.Event | None) -> None:
    """Stop quickly before starting the next expensive operation."""

    if cancel_event and cancel_event.is_set():
        raise ConversionCancelled()


def _converter_subprocess_window_options() -> dict[str, object]:
    """Prevent FFmpeg console windows flashing on Windows GUI builds."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _converter_require_tool_pair(
    directory: Path, executable_suffix: str, require_nonempty: bool = False
) -> tuple[str | None, str | None]:
    """Return the FFmpeg/FFprobe pair from a folder when both are usable.

    ``require_nonempty`` is reserved for the packaged bundle: a zero-length
    executable is treated as a damaged installation. A user-selected folder
    keeps the original existence-only check so an override the user provided
    is reported by the tools themselves rather than by this helper.
    """

    ffmpeg = directory / f"ffmpeg{executable_suffix}"
    ffprobe = directory / f"ffprobe{executable_suffix}"
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        return None, None
    if require_nonempty:
        for candidate in (ffmpeg, ffprobe):
            try:
                if candidate.stat().st_size == 0:
                    return None, None
            except OSError:
                return None, None
    return str(ffmpeg), str(ffprobe)


def _converter_default_tool_runner(command: Sequence[str]) -> tuple[int, str]:
    """Run a tool command, returning its exit code with stdout and stderr."""

    result = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        **_converter_subprocess_window_options(),
    )
    return result.returncode, f"{result.stdout or ''}{result.stderr or ''}"


def _converter_parse_numeric_version(token: str) -> tuple[int, int, int] | None:
    """Extract a comparable ``x.y.z`` from a tool version token."""

    match = _CONVERTER_NUMERIC_VERSION_PATTERN.search(token)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _converter_tool_version(
    runner: ToolRunner, tool_path: str, tool_name: str
) -> tuple[tuple[int, int, int] | None, str, str]:
    """Return (version, banner, configuration) for one FFmpeg-family tool."""

    returncode, output = runner([tool_path, "-version"])
    banner = ""
    configuration = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith(f"{tool_name} version"):
            banner = stripped
        elif stripped.startswith("configuration:") and not configuration:
            configuration = stripped
    if returncode != 0 or not banner:
        return None, banner, configuration
    match = _CONVERTER_TOOL_VERSION_PATTERN.match(banner)
    if not match:
        return None, banner, configuration
    return _converter_parse_numeric_version(match.group("version")), banner, configuration


def _converter_tool_listing(runner: ToolRunner, ffmpeg: str, flag: str) -> str:
    """Return one FFmpeg capability listing or raise a clear error."""

    returncode, output = runner([ffmpeg, "-hide_banner", flag])
    if returncode != 0:
        raise FFmpegCompatibilityError(
            error_messages_format(
                "Checking video tools",
                f"FFmpeg could not list its {flag.lstrip('-')}.",
                "Install the supported FFmpeg build (the packaged application "
                "includes it) and try again.",
                f"ffmpeg: {ffmpeg}\n{_converter_tail(output)}",
            )
        )
    return output


def _converter_listing_contains(listing: str, name: str) -> bool:
    """Return True when an FFmpeg ``-encoders``/``-demuxers`` listing has *name*."""

    for line in listing.splitlines():
        parts = line.split()
        if (
            len(parts) >= 2
            and _CONVERTER_LISTING_FLAGS_PATTERN.match(parts[0])
            and parts[1] == name
        ):
            return True
    return False


def _converter_tail(text: str, line_limit: int = 12) -> str:
    """Return the last few non-empty lines for a diagnostic message."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-line_limit:])
