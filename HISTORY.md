# Project History

This file is append-only. Add new entries at the end; do not rewrite earlier
project history.

## 2026-07-17 — Version 0.1.0

- Started from JaredReabow's fork of `media-to-mkv-converter.sh`.
- Replaced the interactive workflow with the new Mediatovideo Converter Python
  GUI while retaining the original shell proof of concept for reference.
- Added cross-platform scanning, grouping, FFmpeg conversion, progress,
  cancellation, tests, packaging support, and user documentation.

## 2026-07-17 — Application renders and Gist handoff

- Added two privacy-safe interface renders using only fictional state.
- Prepared the original Gist to describe this graphical implementation and
  direct readers to the complete GitHub repository.

## 2026-07-17 — Version 0.2.0

- Added native Windows and macOS prerequisite installers that run before the
  app, install missing Python, Tkinter, FFmpeg, and FFprobe components, and
  verify them before launch.
- Kept a terminal visible throughout startup so installation progress and
  failures never occur without feedback.
- Standardised installation, scan, conversion, and filesystem errors around a
  clear stage, problem, recovery action, and technical detail.
- Added installer/error regression tests and updated the documentation renders.

## 2026-07-17 — Version 0.2.1

- Published the queued performance work for large exports: bounded UI messages
  and logs, throttled high-frequency progress events, and single-pass scanning.
- Preserved final scan/conversion feedback while reducing avoidable memory and
  interface-update pressure.

## 2026-07-18 — Version 0.3.0

- Added a dedicated graphical MKV-to-MP4 workflow without changing the existing
  folder-based `.media` conversion workflow.
- Added H.264/AAC encoding, visible progress, cancellation, overwrite
  protection, partial-file cleanup, clear errors, and output-folder access.
- Added single-file converter tests and updated the privacy-safe application
  renders and documentation.

## 2026-07-18 — Real screenshots and complete functionality guide

- Captured the actual macOS main window and MKV-to-MP4 dialog with blank fields
  so no user paths, filenames, media, photographs, or video frames are exposed.
- Documented every visible control and the complete scan, grouping, naming,
  layout, encoding, progress, cancellation, safety, installer, and error flow.
- Kept the fictional conversion-progress illustration as a clearly labelled
  example rather than presenting it as a real screenshot.


## 2026-10-05 — Version 0.4.0 runtime guards and native packaging

- Replaced import-only compatibility acceptance with package version floors
  and time-bounded Tk/ttk startup probes owned by the runtime module.
- Added video-tool compatibility checks, isolated native bundle resolution,
  self-contained PyInstaller builds, dependency manifests, and native CI.
- Added a unified modular regression harness and real GUI/generated-media
  smoke testing. Unit checks alone are not treated as rendered UI evidence.
- Preserved pre-existing local licensing changes outside the GitHub update.
- Recorded the upstream macOS 27.0 Tk dialog limitation; selected package
  versions are prerequisites rather than a promise against OS regressions.

### Version 0.4.0 packaging verification follow-up

- Replaced the Windows archive command after downloaded artifact inspection
  found TAR contents behind a ZIP filename. The workflow now creates a real
  ZIP and validates its integrity and application executable before upload.

## 2026-10-05 — Version 0.4.1 diagnostic logging

- Added a bounded persistent diagnostic log, early exception capture, and
  saved GUI/conversion activity for debugging blank windows and failed jobs.
- Added a log-opening control and startup/readiness messages with actual
  package versions, log location, and source-versus-package recovery advice.
- Documented log collection in README and extended the integrated harness and
  native frozen smoke tests to verify diagnostic output.

## 2026-10-05 — Version 0.4.2 native Quit and automatic repair

- Deferred the macOS native Quit callback before a modal busy confirmation,
  following the CPython issue 158053 workaround; preserved button picker behavior.
- Added native automatic-repair selection backed by an exact-version local
  complete-app backup, staging verification, detached swap, rollback, and logs.
- Kept source installer recovery separate from packaged-app offline recovery.
- Extended modular regression/native package validation with corruption and
  damaged-package recovery checks; used generated fixtures rather than user media.
- Repaired the original OneDrive checkout's unreadable Git metadata by preserving
  it as `.git-cloud-backup-20261005` and pointing `.git` at verified metadata in
  durable local storage. Preserved tracked files and unrelated license/VS Code edits.
- Kept the local licensing overlays outside the GitHub source update.

### Version 0.4.2 native recovery validation follow-up

- Made repair-lock handover atomic and protected newly created partial locks
  from a concurrent repair attempt.
- Corrected macOS reporter request delivery after an actual popup test exposed
  that LaunchServices does not pass command-line arguments to AppleScript run handlers.
- Added exact Windows Fix automatically/Close labels and native dialog transport
  checks alongside the complete-package recovery harness.

### Version 0.4.2 cross-platform test fixture validation

- Matched recovery fixtures to each platform's packaged layout and executable suffix, after Ubuntu CI exposed macOS-only assumptions.
- Exercised the POSIX helper directly on Linux and retained native Windows PowerShell/dialog checks. The production application code is unchanged.

### Version 0.4.2 native Windows validation follow-up

- Corrected the Windows TaskDialog structure packing against Microsoft's native definitions and recorded native API failure stages for debugging.
- Audited raw ZIP member names before Windows normalizes separators; rejected literal backslashes and NUL bytes.
- Corrected native Windows test setup for helper lock ownership and isolated native dialog selection from mocked fallback tests.

### Version 0.4.2 Windows helper cleanup correction

- Native Windows CI passed the repair dialog button gate and exposed recursive staging-parent cleanup.
- Replaced that cleanup with an atomic empty-directory removal, preserving neighboring files even when the staging layout changes; retained native helper and sibling-preservation checks.

### Version 0.4.2 final Windows fixture isolation

- Gave the shared-staging helper test its own directory name after native Windows CI found a collision with the base application fixture; application code is unchanged.

### Version 0.4.2 detached Windows helper diagnostics

- Full packaged recovery validation found no detached-helper result despite passing direct helper tests.
- Added startup-output retention and an exact production-launch test, covering an empty PATH, apostrophes in paths, and empty optional arguments.
- Retained this diagnostic evidence before choosing the corrective change; did not increase the recovery deadlines.

### Version 0.4.2 Windows launch isolation

- The exact production-launch test reproduced an absent result and empty startup log. Added bounded native probes comparing argument transport, environment, and console flags, with marker controls and cleanup of every fixture child.

### Version 0.4.2 Windows console and module isolation

- Native launch probes showed DETACHED_PROCESS silently skipped even a marker script under both environments. Added focused hidden-console/no-window and one-variable OS environment probes before selecting the production correction.

### Version 0.4.2 Windows private system PATH probe

- Hidden-process probes execute the marker correctly. Added a focused OS-only PATH versus empty-PATH probe to isolate the remaining helper environment difference before changing its policy.

### Version 0.4.2 Windows background repair correction

- Native probes proved CREATE_NO_WINDOW executes the helper, unlike DETACHED_PROCESS, and ruled out empty PATH as the private-profile stall cause.
- Replaced helper cmdlets with direct .NET file, directory, process, time, sleep, and relaunch operations. Preserved atomic results, empty-only cleanup, lock ownership, backup retention, and rollback.
- Kept exact minimal-environment success, rollback, and deadline tests; added silent-exit rejection and removed temporary probe matrices after preserving diagnostic evidence.
