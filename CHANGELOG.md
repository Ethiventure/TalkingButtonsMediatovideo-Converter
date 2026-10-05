# Changelog

All notable changes to Mediatovideo Converter are recorded here.

## [0.4.2] - 2026-10-05

### Added

- Native startup **Fix automatically** action for damaged bundled video tools
  when an exact-version verified offline recovery package is available.
- Complete-app recovery backup, archive integrity/path validation, staged native
  self-test, detached replacement, rollback, and diagnostic repair results.
- Native damaged-package recovery verification in the modular test harness/CI.

### Fixed

- Defer the macOS native Quit callback before entering a modal busy confirmation,
  using the workaround for CPython issue 158053; Cmd-Q now confirms active folder work.
- Match the Windows repair dialog's native structure layout and retain
  diagnostics when the custom dialog API is unavailable.
- Audit original ZIP member names before Windows normalizes separators.
- Remove a repair staging container only when it is empty, preserving
  neighboring files and the retained application backup.
- Retain Windows repair-helper startup output and detect an immediate helper
  exit, so launch failures include an exit code and diagnostic file location.
- Run the Windows helper without a visible console using .NET file/process
  operations, avoiding PowerShell module-loading stalls under a private profile.
- Reject a helper that exits without producing its repair result.

### Limitations

- Offline recovery requires a previous successful packaged startup or explicit
  `--prepare-recovery`; first-run damage still requires a fresh complete install.
- This release does not establish that all future macOS/Tk dialog issues are fixed.

## [0.4.1] - 2026-10-05

### Added

- Persistent rotating diagnostic logs for startup, loaded runtime/tool versions,
  GUI readiness, activity, errors, and exception tracebacks.
- An Open diagnostic log action in the main window and a `--log-path` CLI.
- Terminal startup status and recovery guidance that distinguishes source
  Python/Tk updates from updating a self-contained application package.
- Logging regression tests and native smoke checks that verify the saved log.

## [0.4.0] - 2026-10-05

- Validate the Windows distribution as a real ZIP before upload.

### Changed

- Require Python 3.14.8+, Tk 9.1.0+ on macOS / 9.0.4+ on Windows, and
  FFmpeg/FFprobe 8.1.2+ with the application's required features.
- Probe real Tk list/progress widgets under a deadline before accepting a
  runtime; reject obsolete Apple Tk and broken or stalled installations.
- Validate direct application launches as well as native source launchers.
- Package Python/Tk and video tools into native apps, prefer packaged tools,
  and fail clearly if a native bundle is damaged.
- Pin PyInstaller, record dependency provenance, and add native build CI,
  modular regression coverage, and real GUI/generated-video smoke testing.

### Limitations

- Version checks and self-contained packages cannot guarantee compatibility
  with future operating-system changes. macOS 27.0 has a documented upstream
  Tk dialog issue and requires workflow-specific validation.
=======
## [Unreleased]

Plain English: a new `scripts/dog_filter.py` keeps only the parts of a video
where a dog is on screen. It checks frames on your own Mac, writes a review
list with preview pictures, then builds the dogs-only video after you
approve. Nothing uploads; originals never change.

Technical: two-step CLI (`analyze`/`export`) on the repo `.venv`
(ultralytics YOLOv8s by default at 960px, COCO `dog`); uniform sampling
(default 2 s, exact threshold/merge/handles flags); review artifacts
`segments.csv` + `thumbs/` + `analysis.json`; export via per-segment `-c copy`
and `concat`-copy `+faststart` with atomic rename. Verified on a 6-minute
demo: 248 s kept over 4 segments (spot-checked thumbs show the dog;
a dropped stretch verified dog-free by eye), joined MP4 AV in sync, decoded
audio non-silent (RMS 235).

## [0.3.3] - 2026-10-05

Plain English: pieces keep real, playable sound now, and the joiner takes
MKV pieces too.

- Fixed empty sound tracks. Pieces showed a sound track but played silence,
  because the join step starved the sound while copying the picture. Each
  piece is now cut to an exact measured length instead. If your pieces were
  made with 0.3.1–0.3.2, make them once more — the old ones hold no sound
  and cannot be rescued by joining.
- The joiner accepts MKV pieces as well as MP4 (same picture/sound inside),
  and the output can be MP4 or MKV. A **Creation order** button sorts by file
  time, which recovers event order when filenames are plain numbers like
  `09-24-266`. Tip: choose File naming Month-Day-Category so piece names
  carry the event time themselves.

Technical:

- Per-clip segments dropped `-shortest` for an output `-t
  min(video_frames/fps, audio_bytes/16000)` cut
  (`_converter_segment_cut`; demux now returns the frame count). Rationale:
  copied H.264 packets from raw Annex-B carry no usable timestamps
  (`matroska: Timestamps are unset`), so `-shortest` ended the audio stream
  at ~0 frames while the track header kept a plausible duration; AAC-side
  `Too many bits 16384 > 6144, clamping` noise was a red herring (encoder
  output decodes fine in isolation).
- `converter_join_mp4s`/`converter_collect_mp4s` accept `.mp4`+`.mkv`
  (uniform `h264` + all-`aac`/all-silent gates unchanged, container-agnostic);
  new `converter_order_creation_key` (mtime); dialog gains Creation-order
  sort, MKV save type, and container-neutral wording.
- Verified by decoding, not just probing: rebuilt piece audio 188416 bytes
  RMS 13.9; 5-piece creation-order join MP4 video 364.72 s / audio 365.03 s
  with decoded RMS 280 / peak 18620.

## [0.3.2] - 2026-10-05

Plain English: year folders with extra words now work, and small videos can
be joined back together.

- Folders named like `2021 video` now count as year 2021, so one big pile of
  clips splits into its real days again. Before, 1281 clips landed in a
  single day because only pure `2021` was accepted.
- New “Join MP4s” button. Convert by child folder first (many short, fast
  videos in one folder), then pick that folder and join them into one video.
  The list shows the join order: time order by default, original folder order
  on one click, plus Up/Down to fix anything by hand. Joining copies without
  re-drawing, so it takes minutes and keeps full quality.

Technical:

- `scanner._YEAR_RE` changed from `^\d{4}$` fullmatch to `^(\d{4})\b` prefix
  match; `_scanner_find_date_root` uses the captured group as the year.
  Backward compatible with pure `YYYY` layouts.
- New `converter_join_mp4s` (+ `converter_collect_mp4s`,
  `converter_order_time_key`, `converter_order_folder_key`): recursive
  folder collect in casefold path order; time key
  `(year, month, day, epoch, filename)` parsed from mirror dirs, `MM-DD`
  stems, and embedded 10-digit event epochs; validation (≥2 inputs, `.mp4`
  only, target must not exist, uniform `h264` video + all-`aac`/all-silent
  audio via order-independent `_converter_probe_streams`); single
  `ffmpeg -f concat -c copy -movflags +faststart` through the existing
  atomic-partial/cancel/progress path, reusing `MkvConversionResult`.
- New modal `JoinMp4Dialog` (list with Time/Original/Up/Down, collision-free
  `<folder>-joined.mp4` suggestion, busy guards) + main-window button with
  busy/idle state handling.
- Tests: 2 scanner year-suffix cases, 5 join cases (order keys, recursive
  collect, validation, copy pipeline preserving caller order).

## [0.3.1] - 2026-10-05

### Fixed

- LittlelfSmart `.media` sound is now kept. These files pack picture (types
  0/1) and 8000 Hz 16-bit mono sound (type 3) behind 24-byte headers, which
  FFmpeg alone reads as picture-only. The trailing flag word varies per camera
  (20, 24, 81, ...) and is no longer used to reject files.
- Each clip is now encoded on its own measured frame rate (typically 15-20
  fps from file timestamps) with its own sound, then segments are joined with
  copy. Bulk-joining raw streams caused mostly-silent output and drift across
  hundreds of clips. Files without type-3 chunks still make silent video.
- Added splitter regression tests for LittlelfSmart packing and audio mapping.

## [0.3.0] - 2026-07-18

### Added

- Modal MKV-to-MP4 tool in the main graphical interface.
- MKV input and MP4 destination file selectors with collision-safe suggestions.
- H.264 video, `yuv420p` pixel format, and AAC audio conversion policy.
- Single-file progress reporting, cancellation, partial-file cleanup, output
  folder opening, and structured errors.
- Converter API and regression tests for successful conversion, input
  validation, and overwrite protection.
- Privacy-safe render of the new single-file conversion interface.

### Documentation

- Replaced the primary interface renders with privacy-safe screenshots of the
  actual running macOS application and MKV-to-MP4 dialog.
- Expanded the README with a complete guide to controls, date discovery,
  grouping, layouts, filenames, formats, progress, cancellation, safety,
  installation, and structured errors.

## [0.2.1] - 2026-07-17

### Changed

- Bounded the GUI worker-message queue and activity log so very large camera
  exports cannot grow interface memory without limit.
- Throttled validation and encoding progress events while retaining final
  progress updates.
- Grouped discovered clips during directory traversal instead of retaining a
  second complete list of every source file.
- Updated application renders to show version 0.2.1.

## [0.2.0] - 2026-07-17

### Added

- Windows prerequisite installer using WinGet and Python's install manager.
- macOS prerequisite installer using an existing compatible Python or Homebrew.
- Automatic checks and post-install verification for Python 3.9+, Tkinter,
  FFmpeg, and FFprobe on every launcher start.
- Visible startup terminal progress, installation stages, and recovery actions.
- Structured Stage, Problem, What to do, and Technical detail error messages.
- Installer and error-message regression tests.

### Changed

- Scan, conversion, output-folder, FFmpeg, and direct-launch failures now explain
  both the likely cause and the next action.
- Updated privacy-safe application renders to show version 0.2.0.

### Documentation

- Added automatic and manual prerequisite installation instructions.
- Added privacy-safe application renders made only with fictional interface
  state for the repository and original Gist.

## [0.1.0] - 2026-07-17

### Added

- Cross-platform Tkinter GUI for Windows and macOS.
- Recursive `.media` scanning with `YYYY/MM/DD` recognition.
- Per-day and per-child-folder grouping.
- Mirrored-date and flat output layouts.
- MKV stream-copy and MP4 H.264/AAC conversion modes.
- Visible scan, validation, current-video, and overall progress.
- Safe cancellation, corrupt-file skipping, collision-free naming, and logs.
- PyInstaller build helper, launchers, documentation, and unit tests.
- Launch-time detection of a Python installation that includes Tkinter.
