# Mediatovideo Converter

Mediatovideo Converter is a simple Windows and macOS desktop application for
combining camera `.media` clips into usable video files. It expands the
[original FFmpeg concat gist](https://gist.github.com/priintpar/f7a56af8977206e9f45b486f767b02ac)
with folder-aware grouping, validation, progress reporting, cancellation, and a
graphical interface.

## Installation

The preferred distribution is a **self-contained native app**. The native build
carries its own Python, Tcl/Tk, FFmpeg, FFprobe, and required shared libraries.
It does not need Homebrew, a system Python, or video tools on PATH. Native build
archives are produced by the GitHub **Build native applications** workflow and
are retained as workflow artifacts after all checks pass. They are separate
from GitHub's **Download ZIP**, which contains source code only.

- macOS: extract the archive and open `Mediatovideo Converter.app`.
- Windows: extract the entire archive, keep its files together, and open
  `Mediatovideo Converter.exe` inside the application folder.

For a source checkout, double-click `run_macos.command` or `run_windows.bat`.
These launchers verify the runtime, repair/install missing or outdated
prerequisites, and keep a terminal visible for errors. Their minimum package
versions are Python **3.14.8**, Tk **9.1.0 on macOS** or **9.0.4 on Windows**,
and FFmpeg/FFprobe **8.1.2**. Newer stable versions are accepted only when the
startup checks also pass. The check constructs a Tk window with the actual
list/progress widget types and exercises the event loop under a deadline;
successful `import tkinter` alone is insufficient. Video-tool checks verify
versions and the encoding/demuxing features used by this application.

These package baselines match the selected Homebrew runtime and the official
Windows Python runtime. They are not a guarantee against future OS changes.
In particular, [Python's 3.14.8 release notes](https://www.python.org/downloads/release/python-3148/)
report Tk dialog hangs on macOS 27.0 across current Tk versions. The startup
probe and release smoke tests detect startup/widget failures, but do not prove
that every native file chooser or menu workflow is unaffected. Version 0.4.2
defers the application's native Quit handler until Tk returns from the menu
callback, following the workaround in
[CPython issue 158053](https://github.com/python/cpython/issues/158053). This also
lets Cmd-Q use the running folder-operation confirmation. OS-specific dialog behavior
still needs testing when the operating system or toolkit changes.

## Diagnostic logs and startup status

Every normal application start creates or appends to a UTF-8 diagnostic log.
It records the application version, Python and loaded Tk versions, runtime and
FFmpeg checks, scan/conversion activity, errors with tracebacks, and shutdown.
The main window has an **Open diagnostic log** button. You can also locate the
active log without opening the GUI:

```sh
python run_app.py --log-path
```

For the macOS packaged app, run its native executable from Terminal with
`--log-path`. The Windows package has no console output; use **Open diagnostic
log** or the Windows log location below instead.
The usual log locations are:

| Platform | Diagnostic log |
| --- | --- |
| macOS | `~/Library/Logs/Mediatovideo Converter/application-debug.log` |
| Windows | `%LOCALAPPDATA%\Mediatovideo Converter\Logs\application-debug.log` |
| Linux/source testing | `$XDG_STATE_HOME/mediatovideo-converter/application-debug.log`, or `~/.local/state/mediatovideo-converter/application-debug.log` |

If the normal location is unwritable, the app tries a temporary log location
and reports the path it actually uses. Failure to write a log does not prevent
startup. Logs rotate to keep disk usage bounded; collect the current log and
its numbered backups soon after reproducing an issue. The log can include
local file paths and FFmpeg error details, so review it before sharing it.
It contains no media contents or full environment dump.

Double-clicking `run_macos.command` or `run_windows.bat` keeps a startup terminal
visible. Source runs also mirror diagnostic messages to that terminal;
windowed packages always save them to the log. After the main window has been
drawn, the diagnostic status says:

> Application window initialized; the application should be running now.

The source terminal and diagnostic log also show the log location and runtime
guidance. For a
source checkout, use stable **Python 3.14.8 or newer**, with **Tk 9.1.0 or newer
on macOS** or **Tk 9.0.4 or newer on Windows**. Tkinter is Python's interface to
Tk; the reported Tk patchlevel is the drawing toolkit version actually loaded.
If the source window is blank or fails to open, rerun the launcher so it can
check or upgrade those prerequisites, then collect the diagnostic log.

The self-contained app carries its own Python and Tk. If that app has a problem,
update or reinstall the application package and attach its log; changing system
Python or Tk does not change the bundled runtime. A startup failure shows a
native error dialog with the log location even if Tk cannot open the GUI.
A ready message confirms window initialization, not every later interaction.

To report an issue, reproduce it once, copy the current diagnostic log (and
relevant rotated backups), and include the app version, operating system,
whether you used the package or source launcher, and what you clicked.

## Automatic repair of a damaged packaged app

After the packaged application passes its startup checks, it saves a complete
offline recovery copy outside the application folder. If a later startup finds
missing or incompatible bundled video tools, the native error popup offers
**Fix automatically** when a verified recovery copy exists for that exact app
version, operating system, and CPU architecture. The primary native button is
**Fix automatically** on macOS and Windows. If the Windows custom dialog API is
unavailable, a fallback Yes/No dialog explains that **Yes** runs the repair.
The message identifies the application being repaired.

Repair checks the backup's SHA-256, validates archive paths and symlinks, restores
the complete package to a staging folder, checks its manifest and video-tool
hashes, and runs the app's real GUI/generated-video self-test with PATH cleared.
On macOS it also verifies the restored code signature. Only after those checks
pass does a separate operating-system helper wait for the failed app to exit,
replace it, retain the old copy as a backup, and reopen it. A failed replacement
attempt restores the old copy. Progress and the result are written to the
diagnostic log. The first startup/repair may take longer while files are copied
and checked; the recovery copy uses additional disk space.

On Windows, helper startup output is also saved beside the repair result in
the recovery cache as `recovery-result-<id>.helper-startup.log`. Include this
file with the application diagnostic log when reporting a repair-launch issue.

This is an offline reinstall of the same packaged version. It does not upgrade
the application or change system Python, Tk, Homebrew, or pip packages. If the
app is damaged before its first successful startup, the backup was deleted, the
backup fails validation, or the destination is not writable, install a fresh
complete package. The button cannot recover an executable that cannot start at
all. Source checkouts continue to use the existing installers through
`run_macos.command` or `run_windows.bat`.

The local checksum detects accidental corruption. It is not a trust boundary
against someone who can replace both the backup and its metadata in your user
account. Keep the recovery folder and the old application backup until you have
confirmed the restored app works. No private media files are part of the backup.

To prepare the offline backup explicitly after installing a package, run its
native executable with `--prepare-recovery`. The native CI harness also tests
restoration of a disposable damaged copy; it never damages your installed app.

## Real application screenshots

These are screenshots of the actual macOS application, not interface renders.
The Windows application has the same controls with native Windows styling. The
screenshots were taken with empty fields and contain no user folders, filenames,
media, video frames, or photographs.

![Actual Mediatovideo Converter main window on macOS](docs/app-screenshot-macos.png)

![Actual MKV-to-MP4 window on macOS](docs/mkv-to-mp4-screenshot-macos.png)

## What the application does

Mediatovideo Converter has two workflows:

1. **Folder conversion** recursively finds camera `.media` clips, organises
   them by date and optional child folder, then joins each group into an MKV or
   MP4 video.
2. **Single-file conversion** takes an existing MKV and creates a compatible
   H.264/AAC MP4.

All video processing happens locally with FFmpeg. Nothing is uploaded. The
first-run installer only uses the internet when a required component is
missing.

## Folder conversion: step by step

1. Start the native launcher for the operating system. It checks Python,
   Tkinter, FFmpeg, and FFprobe before opening the app.
2. Choose a **Source** folder containing `.media` files. The app suggests a
   `Converted Videos` folder beside the selected source, which can be changed.
3. Choose the grouping, output layout, video format, and filename style.
4. Click **1. Scan source**. The app shows scan activity, the dates/categories
   it found, clip counts, and the number of videos it will create.
5. Review the scan table, then click **2. Convert videos**.
6. Follow the overall and current-video progress bars, status text, and activity
   log. When complete, click **Open output folder**.

Changing the source or grouping after a scan invalidates the old preview, so
the app clearly asks for another scan before conversion.

## How folders are discovered and grouped

The scanner recognises `YYYY/MM/DD` anywhere below the selected source. This
means the source can be one day folder such as `DCIM/2026/07/17`, the year
folder, `DCIM`, or a higher camera-export folder containing several dates.

For this example:

```text
DCIM/
└── 2026/
    └── 07/
        └── 17/
            ├── Camera 1/
            │   ├── clip-001.media
            │   └── clip-002.media
            └── Camera 2/
                └── clip-001.media
```

- **One video per day** joins all three clips into one video for `2026/07/17`.
- **One video per child folder in each day** makes one video for `Camera 1` and
  one for `Camera 2`. A file placed directly in `17` is put in a `Day root`
  category.

Files outside a recognised date tree are still usable. They are grouped under
the selected source folder, and the scan log reports that their date path was
not recognised. `.media` matching is case-insensitive, and clips are joined in
stable full-relative-path order.

## Every main-window control

| Control | Function |
| --- | --- |
| **Source** | Folder recursively searched for `.media` clips. |
| **Output** | Root folder for completed MKV or MP4 files. Missing date subfolders are created automatically. |
| **FFmpeg bin (optional)** | Manual override for the folder containing both `ffmpeg` and `ffprobe`; normally leave blank to use the installed tools. |
| **Grouping** | Creates one video per date, or one per immediate child folder inside each date. |
| **Output layout** | Recreates `YYYY/MM/DD` below the output folder, or writes every video into one flat folder. |
| **Video format** | Chooses fast MKV stream copy or H.264/AAC MP4 encoding. |
| **File naming** | Chooses `Month-Day` or `Month-Day-Category` filenames. |
| **Scan results** | Previews each date/folder, category, and clip count before any video is written. |
| **Conversion progress** | Shows overall videos, current clip validation/encoding, and a scrolling activity log. |
| **1. Scan source** | Starts a responsive recursive scan and acknowledges scan/cancel/error states. |
| **2. Convert videos** | Starts conversion only after a successful scan and valid output selection. |
| **Cancel** | Requests a safe stop for the active scan or conversion. |
| **MKV → MP4 tool** | Opens the independent single-file converter. |
| **Open output folder** | Opens the selected destination in Finder or File Explorer. |

## Output layout and filenames

With the example date and category above, the choices produce paths like:

| Layout | Naming | Example output |
| --- | --- | --- |
| Replicate dates | Month-Day | `Output/2026/07/17/07-17.mkv` |
| Replicate dates | Month-Day-Category | `Output/2026/07/17/07-17-Camera 1.mkv` |
| Flat folder | Month-Day | `Output/07-17.mkv` |
| Flat folder | Month-Day-Category | `Output/07-17-Camera 1.mkv` |

The category suffix is available when child-folder grouping produces a
category. Characters that are invalid in Windows/macOS filenames are replaced.
The application never overwrites an existing video: if a name already exists,
it safely creates `-2`, `-3`, and so on.

## MKV or MP4 output

**MKV — fast, lossless stream copy** follows the original Gist. It copies the
camera streams without decoding or re-encoding, so it is fast and does not lose
quality. The clips must contain streams and timestamps that FFmpeg can join.
If stream copy cannot join a group, the app recommends trying MP4.

**MP4 — compatible H.264 (slower)** decodes and re-encodes the first available
video and audio streams with H.264 (`libx264`, CRF 20, medium preset) and AAC
(128 kbit/s). It adds fast-start metadata for easier playback. Re-encoding is
slower and lossy, but it can handle many clip/timestamp differences that stream
copy cannot.

A proprietary, damaged, or unrecognised camera stream cannot be repaired merely
by changing its container. The source export must still contain readable media.

## Scanning, progress, cancellation, and safety

- The scan runs outside the UI thread and continuously shows the folder being
  inspected. A completed scan reports clip, day, and planned-video counts.
- Every clip is checked with FFprobe before conversion. Unreadable clips are
  skipped, named in the activity log, and included in the final summary.
- The overall bar tracks video groups. The current bar first tracks validation,
  then FFmpeg encoding time. If duration is unavailable, an animated progress
  bar still shows that work is continuing.
- The status bar and log acknowledge selections, cancelled dialogs, active
  operations, successful outputs, skipped clips, and failures.
- **Cancel** immediately changes the visible status, stops the worker safely,
  keeps already completed videos, and removes the current partial output.
- Outputs are written to a hidden partial file and atomically renamed only after
  FFmpeg succeeds. Existing outputs are never overwritten.
- Accidental window closing during work asks whether the operation should be
  cancelled before exiting.

## Clear error reporting

Input, installer, filesystem, FFmpeg, and unexpected errors are shown with:

- **Stage** — what the app was doing;
- **Problem** — a plain-language explanation;
- **What to do** — the next recovery action;
- **Technical detail** — the relevant path, system message, or FFmpeg detail.

Common cases such as a disconnected source drive, unwritable output folder,
full drive, missing H.264 encoder, unreadable clip, timestamp mismatch, and
missing FFmpeg tools have specific recovery guidance. A failed group does not
silently stop other planned groups.

## Automatic first-run setup

The self-contained application carries its own dependencies and needs no
first-run package installation. For source checkouts, the launchers enforce
Python 3.14.8 or newer, Tk 9.1.0 or newer on macOS (9.0.4 on Windows), and
FFmpeg/FFprobe 8.1.2 or newer. They check these components on every start and
install or upgrade unsupported prerequisites. An internet connection may be
required for that setup.

### Windows

Double-click `run_windows.bat`. A terminal remains visible and explains every
check, installation, verification, and error. Missing components are installed
with Windows Package Manager (WinGet):

- the official Python install manager and Python 3.14 with Tkinter;
- the `Gyan.FFmpeg` package, including FFmpeg and FFprobe.

WinGet is included with supported Windows 10 and Windows 11 systems through
Microsoft App Installer. If WinGet is unavailable, the launcher explains how to
install or update App Installer before continuing.

### macOS

Double-click `run_macos.command`. Its Terminal window checks each component and
keeps all installation progress and errors visible. It uses a compatible
existing Python when possible. Otherwise it installs Homebrew, then the
`python-tk` formula. Missing FFmpeg tools are installed with Homebrew.

Homebrew installation may request the Mac administrator password and explains
what it will change before continuing.

### Manual fallback

The same dependencies can be installed manually. On macOS with Homebrew:

```sh
brew install python-tk
brew install ffmpeg
```

On Windows with WinGet and Python's install manager:

```powershell
winget install 9NQ7512CXL7T -e --accept-package-agreements
py install 3.14
winget install --id Gyan.FFmpeg -e
```

Restart the terminal after installation. If FFmpeg is not on the system path,
use the app's optional **FFmpeg bin** selector to choose the folder containing
`ffmpeg` and `ffprobe` (`.exe` on Windows).

## Run from source

On macOS, double-click `run_macos.command`. To bypass the prerequisite launcher
after everything is installed, run:

```sh
python3 run_app.py
```

On Windows, double-click `run_windows.bat`. To bypass the prerequisite launcher
after everything is installed, run:

```powershell
python run_app.py
```

No Python packages are required to run the source version.

## Build a standalone application

Install the optional build dependency and build on each target operating system:

```sh
python -m pip install -e ".[build]"
python scripts/build_app.py
```

PyInstaller writes the app to `dist/`. Build the Windows `.exe` on Windows and
the macOS app on macOS; PyInstaller does not cross-compile between them. Both
FFmpeg and FFprobe must be available on the build machine; the builder packages
them and their required libraries into the application. See the self-contained
build instructions below for explicit tool selection and validation.

## Convert an existing MKV to MP4

Click **MKV → MP4 tool** in the main window. Choose one `.mkv` input file and
review the suggested `.mp4` destination. The tool:

- suggests the same base filename with `.mp4`, or a numbered/non-conflicting
  alternative if that file already exists;
- lets the destination be changed with a standard Save dialog;
- validates that the source exists and ends in `.mkv` and that the destination
  ends in `.mp4`;
- refuses to overwrite an existing MP4;
- converts the first available video stream to H.264 using the broadly
  compatible `yuv420p` pixel format;
- converts the first available audio stream to AAC at 128 kbit/s;
- adds fast-start metadata for easier playback;
- shows encoding percentage and immediately acknowledges cancellation;
- safely terminates FFmpeg and removes the partial output after cancellation or
  failure;
- enables **Open output folder** after successful conversion.

The folder converter and MKV tool cannot run at the same time, preventing two
operations from competing for the same interface state.

## Privacy-safe illustrated progress example

The real screenshots above intentionally show empty fields. This older
illustration demonstrates a populated scan and progress state using fictional
folders and filenames only; it contains no user media or video frames.

![Illustrated Mediatovideo Converter progress state](docs/conversion-progress.png)

## Tests

```sh
python scripts/test_harness.py
# Also exercise the actual GUI and a generated-video conversion:
python scripts/test_harness.py --integration
# Restore a disposable damaged native app (on its target operating system):
python scripts/test_harness.py --package "/path/to/Mediatovideo Converter.app"
```

The test suite covers date discovery, day/child grouping, fallback layouts,
portable naming, collision handling, folder and single-file FFmpeg
orchestration, error clarity, both native installer contracts, deferred macOS
Quit handling, and automatic-repair integrity and failure paths.


## Building the self-contained app

Build on the target operating system and CPU architecture, using a runtime
that passes the same checks as startup. A virtual environment alone does not
supply a separate Tk installation. PyInstaller packages the validated Python
and Tk libraries into the native application.

```sh
python -m pip install '.[build]'
python scripts/build_app.py --ffmpeg-bin /path/to/ffmpeg/bin
```

The build requires both FFmpeg and FFprobe, records package versions, binary
hashes, and tool build configuration in `BUILD-MANIFEST.json`, and includes
third-party notices. Review the dependency licenses and corresponding-source
requirements before redistributing a build. The existing application license
still applies independently of the bundled tools' licenses.

The native workflow runs the modular regression harness, builds the app, and
runs its `--self-test` with an empty PATH. This realizes the actual main window
and MKV-to-MP4 dialog and converts generated media; no personal videos are used.
On Windows, keep the entire packaged application folder together. On macOS,
keep the `.app` bundle intact. A damaged bundle produces a clear startup error
and never silently switches to system video tools. A deliberately selected
FFmpeg folder remains an explicit override and is checked before use.

For a diagnostic run of either the source launcher or the native executable:

```sh
python run_app.py --check-runtime
python run_app.py --self-test --self-test-report build/self-test.json
```

For a native app, substitute its executable for `python run_app.py`. Always
impose an overall timeout when automating GUI smoke tests; the release workflow
does so. Windowed startup failures are written to `application-debug.log`; see the
diagnostic log instructions above.
