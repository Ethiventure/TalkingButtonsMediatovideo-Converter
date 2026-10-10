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
=======
## 2026-10-05 — Day log: sound recovery, dates, joining, dog filter

Work by Muse Spark (Meta's model) in opencode for Ethiventure. The owner
keeps talking-button dog videos from a LittlelfSmart motion camera. Read top
to bottom: each step caused the next. Versions moved 0.3.0 → 0.3.1 → 0.3.2 →
0.3.3 → Unreleased (dog filter); tests moved 15 → 18 → 25 → 28, green
throughout. Each step below has plain words first, then technical detail,
then the proof that it worked.

### 1. The app window opened blank

Plain: the Mac's old drawing kit could not draw the app, so the window came
up empty. Installing the new kit through Homebrew fixed it.

Technical: system Python 3.9.6 ships Tk 8.5 (deprecated); ran
`brew install python-tk` (3.14.8_1), so the launcher picks
`/opt/homebrew/bin/python3` with Tk 9.1. Note: Tk windows hang in headless
agent sessions (no display) — expected, not a bug.

Proof: prerequisites all OK, app code imports, 15/15 tests green.

### 2. Converted videos had no sound

Plain: the app made silent videos, but the camera's own app played sound. So
the sound was hiding inside the files in a shape normal tools cannot see.

Technical: `ffprobe` showed video-only (`h264`) on sources and outputs. A hex
look found a 24-byte header per chunk —
`[u32 type][u32 len][u64 timestamp_ms][u32 ?][u32 ?]` — with types 0/1
carrying H.264 video and type 3 carrying 640-byte 8000 Hz 16-bit mono PCM
(~40 ms per chunk). FFmpeg's H264 reader skips type-3 chunks as invalid data.

Proof: 20 sampled clips, all video-only to `ffprobe`, all carrying type-3
sound underneath.

### 3. First sound fix, then its two bugs (v0.3.1)

Plain: the first splitter worked on two test clips but failed almost
everything in a full run, and joining thousands of clips in one go let sound
slide away from picture. Both got fixed.

Technical: (a) the splitter only accepted one flag value (`tail == 20`;
cameras send 20/24/81, ...) — gate relaxed to header size, sane lengths, and
types 0/1/3. (b) Bulk raw concatenation replaced with per-clip encode on
measured frame rate (typically 15–20 fps, clamped 10–30) plus copy-join.

Proof: 18/18 tests; the full run's 9 minutes of sound out of 2.8 hours
matched the old gate's ~1-in-15 pass rate exactly.

### 4. Real dates and a joiner (v0.3.2)

Plain: folders named `2021 video` did not count as a year, so 1281 clips fell
into one pile. They count now. A Join button was added so many short, fast
piece videos can be merged into day videos in a shown, fixable order.

Technical: scanner year check `^\d{4}$` → `^(\d{4})\b`; new
`converter_join_mp4s` + `converter_collect_mp4s` + time/folder order keys +
order-independent stream probing (real `ffprobe` prints name-before-type);
new modal `JoinMp4Dialog` (Time / Original / Up / Down list).

Proof: 2021 source went 1 pile → 4 days / 374 event groups (2022: 7 days /
787 groups); 25/25 tests; 3-piece demo joined at 91.87 s video / 91.97 s
audio.

### 5. The MKV pieces looked wrong but were fine

Plain: the owner's 374 pieces came out as MKV with odd names like
`09-24-266`. That was two settings, not broken code: the format box was left
on MKV (fast and fine), and Month-Day naming numbers collisions instead of
naming events. Creation order still equals event order.

Technical: verified oldest file `09-24.mkv` through newest `09-24-299.mkv`
follow conversion (hence event) order. Advice recorded: Month-Day-Category
naming for meaningful names; do not move/copy pieces before joining.

### 6. Ghost soundtracks (v0.3.3)

Plain: pieces listed a sound track but played silence. Each piece is now cut
to an exact measured length, so sound survives. The joiner also takes MKV
and can sort by creation time. Pieces made before this fix hold no sound and
must be re-made.

Technical: `-c:v copy` from raw Annex-B leaves packets without usable
timestamps (`matroska: Timestamps are unset`), under which `-shortest` ended
the AAC stream at ~0 frames while the track header kept a plausible duration.
Fix: output `-t min(frames/fps, audio_bytes/16000)` via
`_converter_segment_cut` (demux now returns the frame count). The AAC-side
`Too many bits, clamping` warning was a red herring (isolated encodes decode
1:1). Join went container-agnostic (`.mp4`+`.mkv`). Standing lesson: verify
sound by decoding, never by listing tracks.

Proof: rebuilt piece audio decodes (188416 bytes, RMS matches source PCM);
5-piece join decoded RMS 280 / peak 18620; 28/28 tests.

### 7. Outside tools checked before building (dog filter decision)

Plain: three suggested apps were checked live. None fits the job — one is a
general editor needing a paid AI key, one finds moments but cannot edit, one
is young and Windows-first. So a small local script got built instead. Scene
detection was left out on purpose: a still sensor camera gains nothing from
it; even sampling wins.

Technical: OpenReel exists twice under one name (browser `Augani/openreel-video`,
MIT ~5k stars; desktop `openreelio/openreelio`, 19 stars, v0.1.0) plus a token
promo on its site. VideoHighlighter (`Aseiel/VideoHighlighter`, AGPL-3.0,
~132 stars) is closest but immature. Edit Mind (`IliasHad/edit-mind`, ~1.8k
stars) is search-only, Docker-based, self-described not production-ready,
custom licence. One licence lookup failed transiently; YOLO licence flagged
as check-at-install (believed AGPL, fine for home use).

### 8. Dog filter script (Unreleased)

Plain: a command-line helper keeps only the parts of a video where a dog is
on screen. It works in two steps — find moments and write a review list with
preview pictures, then build the video after approval. Originals never change
and nothing uploads. The tiny detector kept missing the small dark dog, so a
stronger one at higher detail became the default. Review pictures were checked
by eye; one dropped stretch was confirmed genuinely dog-free.

Technical: `scripts/dog_filter.py` (`analyze` → `segments.csv` + `thumbs/` +
`analysis.json`, then `export` via per-segment `-c copy` and concat-copy
`+faststart` with atomic rename and overwrite refusal). Repo `.venv`
(gitignored) holds ultralytics 8.4.173; weights in `~/.cache/dog_filter/`.
Tuning on 182 demo frames: nano/640 → 29 hits; small/640 → 59–62; small/960 →
75–79. Defaults: yolov8s, imgsz 960, conf 0.25, merge-gap 10, handles 3, plus
`--imgsz`/`--model`/`--every` flags. Demo result: 248/365 s kept over 4
segments; export decoded non-silent (RMS 235). Cost guide: ~10 min background
work per hour of footage.

### 9. README and docs

Plain: the top of the README now describes the talking-button `.media` job
(the original gist sentence kept word for word). A full instructions audit
checked every step against the real files and fixed five stale points,
including a brand-new dog-filter setup section. A per-project notebook
(`docs/things-taught.md`) keeps each structural lesson in plain words.

Technical: audit fixes were the workflow count (two → four), the Join button
rename, MKV picture-stream wording, the missing dog-filter setup, and the
test-coverage line. Verified live: gist link, WinGet IDs against
`install_windows.ps1`, all screenshots present, tests green on system Python
3.9 and brew 3.14. Screenshots predate the Join button (agent sessions have no
screen to retake them) — the README says so.

### 10. Security sweep and scrub

Plain: the whole repo was checked file by file. No personal videos, no
secrets, no personal paths anywhere; history never held any. The two dog
names and one real folder name were removed wherever they had slipped in.
One leftover dead function was deleted.

Technical: 32 tracked files; repo-wide searches for names, usernames, and
local/temp paths return zero; `git log -S` clean; `.venv` (1.1 GB) ignored.
Removed `_converter_demux_group` (unused since the per-clip rewrite) and a personal year-video folder name used as a test fixture (now `"Camera exports"`). Kept upstream
`JaredReabow` provenance (public, not personal). Playback demos for the owner
live outside the repo: `littlelf-test-with-sound.mp4`,
`joined-3-events-demo.mp4`, `littlelf-mixed-8clips-test.mp4`,
`joined-5-events-fixed.mp4`, `dogs-only-demo.mp4`.
