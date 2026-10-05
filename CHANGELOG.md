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
