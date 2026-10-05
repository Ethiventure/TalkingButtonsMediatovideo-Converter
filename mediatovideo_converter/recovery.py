"""Verified local recovery cache for the packaged application.

The frozen application seeds a cache of itself, validates content before
trusting it, and can rebuild its installation from that cache with a detached
helper. Nothing here downloads anything: this project has no released FFmpeg
source distribution to point at, so recovery is deliberately local-only and the
cache lives beside the user's application data, never inside the bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from . import __version__

APPLICATION_DIRECTORY_NAME = "MediatovideoConverter"
RECOVERY_CACHE_DIRECTORY_NAME = "recovery-cache"
RECOVERY_ARCHIVE_NAME = "mediatovideo-recovery.zip"
RECOVERY_METADATA_NAME = "recovery.json"
RECOVERY_METADATA_SCHEMA = 1

# Strict, explicit bounds. A recovery cache larger than these is treated as
# damage rather than something to unpack.
RECOVERY_MAX_ARCHIVE_BYTES = 2 * 1024**3
RECOVERY_MAX_UNCOMPRESSED_BYTES = 6 * 1024**3
RECOVERY_MAX_ARCHIVE_MEMBERS = 50_000
RECOVERY_SELF_TEST_TIMEOUT_SECONDS = 120
RECOVERY_HELPER_WAIT_SECONDS = 900
# A lock that cannot be read yet is only treated as stale after this grace
# window, so a writer in the middle of the atomic handover is never mistaken
# for a dead owner.
RECOVERY_LOCK_GRACE_SECONDS = 10

_MANIFEST_NAME = "BUILD-MANIFEST.json"
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class RecoveryError(RuntimeError):
    """Raised internally when recovery cannot proceed safely."""


def _recovery_log(message: str, **fields: Any) -> None:
    """Append to the diagnostic log through the public diagnostics API."""

    try:
        from . import diagnostics

        diagnostics.diagnostics_info(message, **fields)
    except Exception:  # noqa: BLE001 - logging must never break recovery
        pass


def _recovery_identity() -> dict[str, str]:
    """Return the version, platform and machine a cache must match exactly."""

    return {
        "version": __version__,
        "platform": sys.platform,
        "machine": platform.machine(),
    }


def _recovery_is_frozen() -> bool:
    """Return True when running as a packaged application."""

    return bool(getattr(sys, "frozen", False))


def _recovery_app_support_root() -> Path:
    """Return the per-user application data directory outside the bundle."""

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) if base else Path.home() / ".local" / "share"


def _recovery_cache_directory() -> Path:
    """Return the version, platform and architecture keyed cache directory.

    Keying by identity keeps the previous release's cache intact when the user
    upgrades, instead of replacing the only known-good copy.
    """

    identity = _recovery_identity()
    key = "-".join(
        (identity["version"], identity["platform"], identity["machine"])
    )
    safe_key = re.sub(r"[^A-Za-z0-9._-]", "-", key) or "unknown"
    return (
        _recovery_app_support_root()
        / APPLICATION_DIRECTORY_NAME
        / RECOVERY_CACHE_DIRECTORY_NAME
        / safe_key
    )


def _recovery_archive_path() -> Path:
    """Return the cached recovery archive path."""

    return _recovery_cache_directory() / RECOVERY_ARCHIVE_NAME


def _recovery_metadata_path() -> Path:
    """Return the cache metadata path."""

    return _recovery_cache_directory() / RECOVERY_METADATA_NAME


def _recovery_result_path(operation_id: str) -> Path:
    """Return the per-operation file the detached helper writes its result to.

    A fresh name per repair means a stale result can never be mistaken for the
    outcome of the repair that is running now, even when two copies of the
    application share one cache directory.
    """

    return _recovery_cache_directory() / f"recovery-result-{operation_id}.json"


def _recovery_operation_id() -> str:
    """Return a unique id for one repair operation."""

    return os.urandom(8).hex()


def _recovery_lock_path() -> Path:
    """Return the single repair lock shared by every copy of the app."""

    return _recovery_cache_directory() / "repair.lock"


def _recovery_acquire_lock(operation_id: str) -> tuple[bool, str]:
    """Take the repair lock, clearing only a lock whose owner has exited."""

    lock = _recovery_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in (1, 2):
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            owner = _recovery_lock_owner(lock)
            if owner:
                if _recovery_pid_alive(owner):
                    return False, "A repair is already in progress; wait for it to finish."
                lock.unlink(missing_ok=True)
                continue
            # An unreadable or partially written lock is only cleared once it is
            # older than the grace window.
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                return False, "A repair is already in progress; wait for it to finish."
            if age < RECOVERY_LOCK_GRACE_SECONDS:
                return False, "A repair is already in progress; wait for it to finish."
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(_recovery_lock_text(os.getpid(), operation_id))
        return True, "repair lock acquired"
    return False, "A repair is already in progress; wait for it to finish."


def _recovery_lock_text(pid: int, operation_id: str) -> str:
    """Return the lock body.

    The first line is ``pid=<n>`` so the detached shell and PowerShell helpers
    can compare it with their own process id without a parser.
    """

    return (
        f"pid={pid}\n"
        f"operation={operation_id}\n"
        f"started_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
    )


def _recovery_lock_owner(lock: Path) -> int:
    """Return the process id recorded in a lock file, or 0."""

    try:
        first_line = lock.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError, ValueError):
        return 0
    if not first_line.startswith("pid="):
        return 0
    try:
        return int(first_line[4:].strip())
    except ValueError:
        return 0


def _recovery_release_lock() -> None:
    """Release the repair lock when no helper was started."""

    _recovery_lock_path().unlink(missing_ok=True)


def _recovery_target_path() -> Path | None:
    """Return the installed application this process runs from.

    On macOS the running executable lives in ``App.app/Contents/MacOS``, so the
    ``.app`` ancestor is the recoverable package. On Windows the executable's
    own directory is the package.
    """

    if not _recovery_is_frozen():
        return None
    executable = Path(sys.executable)
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
        return None
    return executable.parent


def recovery_target_text() -> str:
    """Return the installed application path as text, or "" when unknown.

    The failure reporter shows this to the user, so the target-path policy
    stays in this module instead of being duplicated elsewhere.
    """

    target = _recovery_target_path()
    return str(target) if target is not None else ""


def _recovery_manifest_path(bundle_root: Path) -> Path:
    """Return the provenance manifest inside a bundle or staged copy."""

    if sys.platform == "darwin":
        return bundle_root / "Contents" / "Resources" / _MANIFEST_NAME
    return bundle_root / _MANIFEST_NAME


def _recovery_tools_root(bundle_root: Path) -> Path:
    """Return the packaged video-tools directory inside a bundle."""

    from .converter import converter_bundled_tools_dirname

    directory_name = converter_bundled_tools_dirname()
    if sys.platform == "darwin":
        return bundle_root / "Contents" / "Frameworks" / directory_name
    return bundle_root / "_internal" / directory_name


def _recovery_read_manifest(bundle_root: Path) -> dict[str, Any] | None:
    """Read a bundle manifest, returning None when it is missing or unreadable."""

    try:
        payload = json.loads(_recovery_manifest_path(bundle_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _recovery_identity_matches(manifest: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether a manifest describes exactly this build identity."""

    application = manifest.get("application") if isinstance(manifest.get("application"), Mapping) else {}
    build = manifest.get("build") if isinstance(manifest.get("build"), Mapping) else {}
    expected = _recovery_identity()
    found = {
        "version": str(application.get("version") or ""),
        "platform": str(build.get("platform") or ""),
        "machine": str(build.get("machine") or ""),
    }
    for name, value in expected.items():
        if found[name] != value:
            return False, (
                f"manifest {name} is {found[name] or 'missing'}, expected {value}"
            )
    return True, "manifest identity matches"


def _recovery_tool_hashes(manifest: Mapping[str, Any], tools_root: Path) -> tuple[bool, str]:
    """Verify the packaged video tools against the manifest digests.

    The build writes the packaged digests to the manifest's ``hashes`` object
    (the ``tools`` object holds the version report), so that is the field that
    must match; older manifests that carried the digest under ``tools`` are
    still accepted.
    """

    hashes = manifest.get("hashes") if isinstance(manifest.get("hashes"), Mapping) else {}
    reports = manifest.get("tools") if isinstance(manifest.get("tools"), Mapping) else {}
    suffix = ".exe" if sys.platform == "win32" else ""
    for name in ("ffmpeg", "ffprobe"):
        digest = str(hashes.get(name) or "")
        if not digest:
            entry = reports.get(name) if isinstance(reports.get(name), Mapping) else {}
            digest = str(entry.get("sha256") or "")
        if not digest:
            return False, f"manifest has no {name} digest"
        candidate = tools_root / f"{name}{suffix}"
        if not candidate.is_file():
            return False, f"packaged {name} is missing"
        if _recovery_sha256(candidate) != digest:
            return False, f"packaged {name} does not match the manifest digest"
    return True, "packaged tools match the manifest"


def _recovery_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recovery_directory_bytes(path: Path) -> int:
    """Return the total size of the regular files below a directory."""

    total = 0
    for root, _directories, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(Path(root) / name).st_size
            except OSError:
                continue
    return total


def _recovery_write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON through a temporary sibling so a failure cannot corrupt it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _recovery_codesign_verify(bundle_root: Path) -> tuple[bool, str]:
    """Verify the macOS code signature, which is a no-op elsewhere."""

    if sys.platform != "darwin":
        return True, "code signing is not used on this platform"
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle_root)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"code signature could not be checked: {error}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-400:]
        return False, f"code signature is not valid: {detail}"
    return True, "code signature is valid"


def _recovery_cache_status() -> tuple[bool, str, dict[str, Any]]:
    """Return whether the cache metadata and archive are valid and matching."""

    metadata_path = _recovery_metadata_path()
    archive_path = _recovery_archive_path()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "no valid recovery cache metadata", {}
    if not isinstance(metadata, dict) or metadata.get("schema") != RECOVERY_METADATA_SCHEMA:
        return False, "recovery cache metadata has an unsupported schema", {}
    identity = _recovery_identity()
    for name, value in identity.items():
        if str(metadata.get(name) or "") != value:
            return False, f"recovery cache {name} is {metadata.get(name) or 'missing'}", {}
    if not archive_path.is_file():
        return False, "the recovery archive is missing", {}
    if archive_path.stat().st_size > RECOVERY_MAX_ARCHIVE_BYTES:
        return False, "the recovery archive is larger than the supported limit", {}
    if _recovery_sha256(archive_path) != str(metadata.get("archive_sha256") or ""):
        return False, "the recovery archive does not match its recorded digest", {}
    return True, "recovery cache is valid", metadata


def _recovery_is_packaged_tools_failure(failure: object) -> bool:
    """Return True only for the canonical packaged video-tools failure.

    The failure reporter passes the original failure block as text, so the
    canonical ``Stage: Checking packaged video tools`` marker from the error
    formatter is matched literally. A generic ``Checking video tools`` stage is
    deliberately not matched, because that also covers a manually chosen
    folder, which a cached copy of this app cannot repair.
    """

    if isinstance(failure, BaseException):
        return type(failure).__name__ in {
            "FFmpegNotFoundError",
            "FFmpegCompatibilityError",
        }
    if isinstance(failure, str):
        lowered = failure.casefold()
        return "checking packaged video tools" in lowered or "packaged video tools" in lowered
    return False


def _recovery_backup_path(target: Path) -> Path:
    """Return a fresh unused backup sibling so repairs never overwrite one."""

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for _attempt in range(100):
        suffix = os.urandom(4).hex()
        candidate = target.with_name(f"{target.name}.previous-{stamp}-{suffix}")
        if not candidate.exists():
            return candidate
    raise RecoveryError("a free backup name could not be found next to the application")


def _recovery_pid_alive(pid: int) -> bool:
    """Return True when a process with this id still exists.

    ``os.kill`` is never used for this on Windows: with any signal other than
    CTRL_C_EVENT or CTRL_BREAK_EVENT it terminates the target process, so the
    Win32 API is queried instead.
    """

    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _recovery_windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _recovery_windows_pid_alive(pid: int) -> bool:
    """Query process liveness through OpenProcess/GetExitCodeProcess."""

    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _recovery_audit_archive(archive: Path) -> tuple[bool, str, int]:
    """Reject an archive that could write outside its extraction directory.

    Traversal, absolute paths, drive letters, backslash separators, duplicate
    names, special files, symlinks that leave the archive and any member that
    lives under a symlinked directory are all rejected, together with member
    count and size caps.
    """

    if archive.stat().st_size > RECOVERY_MAX_ARCHIVE_BYTES:
        return False, "the recovery archive is larger than the supported limit", 0
    try:
        handle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        return False, f"the recovery archive is not a valid ZIP file: {error}", 0
    with handle:
        members = handle.infolist()
        if len(members) > RECOVERY_MAX_ARCHIVE_MEMBERS:
            return False, "the recovery archive contains too many entries", 0
        seen: set[str] = set()
        symlinks: set[str] = set()
        total = 0
        # First pass: validate every member and every symlink lexically. A
        # packaged macOS app legitimately links inside itself (for example
        # Resources/x -> ../../Frameworks/x), so internal ".." is allowed while
        # a target outside the archive's top-level package is not.
        for member in members:
            name = member.filename
            if name in seen:
                return False, f"the recovery archive repeats the entry {name}", 0
            seen.add(name)
            if "\\" in name:
                return False, f"the recovery archive uses a backslash path: {name}", 0
            if name.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(name):
                return False, f"the recovery archive has an absolute path: {name}", 0
            parts = PurePosixPath(name).parts
            if ".." in parts:
                return False, f"the recovery archive escapes its directory: {name}", 0
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                if sys.platform == "win32":
                    return False, "symlinks are not allowed in a Windows recovery archive", 0
                target = handle.read(member).decode("utf-8", "replace")
                resolved = _recovery_resolve_link(PurePosixPath(name).parent, target)
                if resolved is None or not resolved.parts or resolved.parts[0] != parts[0]:
                    return False, f"the recovery archive links outside its package: {name}", 0
                symlinks.add(name)
            elif stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
                return False, f"the recovery archive contains a special file: {name}", 0
            total += member.file_size
            if total > RECOVERY_MAX_UNCOMPRESSED_BYTES:
                return False, "the recovery archive expands beyond the supported limit", 0
            if member.file_size and member.compress_size == 0:
                return False, f"the recovery archive has an invalid entry: {name}", 0
        # Second pass: a member must never live under a symlinked directory,
        # whatever order the entries appear in.
        for member in members:
            parts = PurePosixPath(member.filename).parts
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) in symlinks:
                    return False, (
                        "the recovery archive places files under a symlinked "
                        f"directory: {member.filename}"
                    ), 0
    return True, "the recovery archive passed its safety checks", total


def _recovery_resolve_link(link_parent: PurePosixPath, target: str) -> PurePosixPath | None:
    """Resolve a symlink target lexically, or None when it escapes upward."""

    if target.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(target):
        return None
    resolved = link_parent
    for part in PurePosixPath(target).parts:
        if part == ".":
            continue
        if part == "..":
            if not resolved.parts:
                return None
            resolved = resolved.parent
            continue
        resolved = resolved / part
    return resolved
def _recovery_extract_archive(archive: Path, destination: Path) -> None:
    """Extract an audited archive, preserving macOS framework symlinks."""

    destination.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", str(archive), str(destination)],
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
        if result.returncode != 0:
            raise RecoveryError(
                "the recovery archive could not be extracted: "
                f"{(result.stderr or result.stdout or '').strip()[-400:]}"
            )
        return
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)


def _recovery_find_bundle_root(stage_root: Path) -> Path | None:
    """Return the application package inside an extracted staging directory."""

    for child in sorted(stage_root.iterdir()):
        if not child.is_dir():
            continue
        if _recovery_manifest_path(child).is_file():
            return child
    return None


def _recovery_staged_executable(bundle_root: Path) -> Path | None:
    """Return the launchable executable inside a staged bundle."""

    if sys.platform == "darwin":
        candidates = sorted((bundle_root / "Contents" / "MacOS").glob("*"))
        return candidates[0] if candidates else None
    candidates = sorted(bundle_root.glob("*.exe"))
    return candidates[0] if candidates else None


def _recovery_run_self_test(bundle_root: Path) -> tuple[bool, str]:
    """Run the staged application's native self-test with an empty PATH.

    A zero exit code alone is not proof: the report must also say it ran
    frozen, saw an empty PATH, and passed its own diagnostics, GUI and video
    checks, so a partial or forged report cannot be mistaken for success.
    """

    executable = _recovery_staged_executable(bundle_root)
    if executable is None or not executable.is_file():
        return False, "the staged application has no executable"
    report = Path(tempfile.mkdtemp(prefix="mediatovideo-recovery-selftest-")) / "report.json"
    environment = dict(os.environ)
    environment["PATH"] = ""
    try:
        result = subprocess.run(
            [str(executable), "--self-test", "--self-test-report", str(report)],
            capture_output=True,
            text=True,
            check=False,
            timeout=RECOVERY_SELF_TEST_TIMEOUT_SECONDS,
            env=environment,
            cwd=str(bundle_root),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"the staged application self-test could not run: {error}"
    if not report.is_file():
        return False, (
            "the staged application wrote no self-test report "
            f"(exit {result.returncode})"
        )
    if result.returncode != 0:
        return False, f"the staged self-test exited {result.returncode}"
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return False, f"the staged self-test report could not be read: {error}"
    if not isinstance(payload, dict):
        return False, "the staged self-test report is not an object"
    if payload.get("ok") is not True:
        return False, "the staged application failed its self-test"
    if payload.get("frozen") is not True:
        return False, "the staged self-test did not run as a packaged application"
    if payload.get("path"):
        return False, "the staged self-test saw an inherited PATH entry"
    for name in ("diagnostics", "gui", "video"):
        if not isinstance(payload.get(name), Mapping) or not payload[name]:
            return False, f"the staged self-test reported no {name} result"
    return True, "the staged application passed its self-test"


def _recovery_verify_staged(bundle_root: Path) -> tuple[bool, str]:
    """Verify identity, tool hashes, signature and self-test of a staged copy."""

    manifest = _recovery_read_manifest(bundle_root)
    if manifest is None:
        return False, "the staged application has no provenance manifest"
    matches, detail = _recovery_identity_matches(manifest)
    if not matches:
        return False, f"the staged application does not match this build: {detail}"
    matches, detail = _recovery_tool_hashes(manifest, _recovery_tools_root(bundle_root))
    if not matches:
        return False, f"the staged application failed its tool check: {detail}"
    matches, detail = _recovery_codesign_verify(bundle_root)
    if not matches:
        return False, f"the staged application failed its signature check: {detail}"
    return _recovery_run_self_test(bundle_root)


def _recovery_write_helper(directory: Path, relaunch: bool) -> Path:
    """Write the detached swap helper for this platform."""

    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        helper = directory / "mediatovideo-repair.sh"
        helper.write_text(_POSIX_HELPER, encoding="utf-8")
        helper.chmod(0o700)
        return helper
    helper = directory / "mediatovideo-repair.ps1"
    helper.write_text(_POWERSHELL_HELPER, encoding="utf-8")
    return helper


def _recovery_launch_helper(helper: Path, arguments: Sequence[str]) -> int:
    """Start the helper detached and return its process id.

    The arguments are raw argv values handed straight to ``Popen``: no shell is
    involved, so quoting them here would become literal quote characters in the
    paths the helper receives.
    """

    environment = dict(os.environ)
    environment["RECOVERY_RESULT_TARGET_JSON"] = json.dumps(arguments[2])
    environment["RECOVERY_RESULT_BACKUP_JSON"] = json.dumps(arguments[3])
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
    }
    if sys.platform == "darwin":
        process = subprocess.Popen(
            ["/bin/sh", str(helper), *arguments], start_new_session=True, **options
        )
        return process.pid
    powershell = _recovery_windows_powershell()
    process = subprocess.Popen(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
            *arguments,
        ],
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        **options,
    )
    return process.pid


def _recovery_handover_lock(helper_pid: int, operation_id: str) -> None:
    """Give the lock to the helper so a running repair is never seen as stale."""

    if not isinstance(helper_pid, int) or helper_pid <= 0:
        # Without a helper pid the parent-owned lock stays; it becomes stale
        # once this process exits and the next repair clears it.
        return
    lock = _recovery_lock_path()
    temporary = lock.with_name(f".{lock.name}.{os.urandom(4).hex()}.tmp")
    try:
        # Write the complete handover body to a unique sibling and swap it in,
        # so a reader never observes a truncated pid.
        temporary.write_text(
            _recovery_lock_text(helper_pid, operation_id), encoding="utf-8"
        )
        os.replace(temporary, lock)
    except OSError:
        # The lock only prevents a duplicate repair; losing the handover is not
        # fatal because the parent pid becomes stale once this process exits.
        temporary.unlink(missing_ok=True)


def _recovery_windows_powershell() -> Path:
    """Return the absolute Windows PowerShell shipped with the OS."""

    system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def recovery_seed() -> dict[str, Any]:
    """Cache a verified copy of the running packaged application.

    Call this only after the runtime and video-tool preflight succeeded. The
    running bundle is verified before anything is written, an existing valid
    cache is kept, and the source application is only ever read.
    """

    if not _recovery_is_frozen():
        return {
            "ok": False,
            "detail": "Recovery caching is only available in the packaged application. "
            "Start the app with run_macos.command or run_windows.bat instead.",
        }
    target = _recovery_target_path()
    if target is None or not target.is_dir():
        return {"ok": False, "detail": "The packaged application location could not be found."}

    valid, detail, _metadata = _recovery_cache_status()
    if valid:
        return {"ok": True, "detail": "A valid recovery cache already exists."}

    manifest = _recovery_read_manifest(target)
    if manifest is None:
        return {"ok": False, "detail": "The application manifest is missing; recovery was not cached."}
    matches, identity_detail = _recovery_identity_matches(manifest)
    if not matches:
        return {"ok": False, "detail": f"The running application is not the expected build: {identity_detail}."}
    matches, tools_detail = _recovery_tool_hashes(manifest, _recovery_tools_root(target))
    if not matches:
        return {"ok": False, "detail": f"The packaged video tools are damaged: {tools_detail}."}
    matches, signing_detail = _recovery_codesign_verify(target)
    if not matches:
        return {"ok": False, "detail": f"The application signature is not valid: {signing_detail}."}

    cache_directory = _recovery_cache_directory()
    cache_directory.mkdir(parents=True, exist_ok=True)
    archive_path = _recovery_archive_path()
    temporary = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.tmp")
    try:
        _recovery_build_archive(target, temporary)
        os.replace(temporary, archive_path)
    except (OSError, RecoveryError) as error:
        temporary.unlink(missing_ok=True)
        return {"ok": False, "detail": f"The recovery cache could not be created: {error}."}

    payload = {
        "schema": RECOVERY_METADATA_SCHEMA,
        **_recovery_identity(),
        "archive": archive_path.name,
        "archive_sha256": _recovery_sha256(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "uncompressed_bytes": _recovery_directory_bytes(target),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _recovery_write_json_atomic(_recovery_metadata_path(), payload)
    except OSError as error:
        return {"ok": False, "detail": f"The recovery cache metadata could not be written: {error}."}

    valid, detail, _metadata = _recovery_cache_status()
    if not valid:
        return {"ok": False, "detail": f"The recovery cache failed its check: {detail}."}
    _recovery_log("Recovery cache seeded", version=payload["version"])
    return {"ok": True, "detail": "The recovery cache is ready."}


def _recovery_build_archive(target: Path, destination: Path) -> None:
    """Create the recovery archive without modifying the source bundle."""

    if sys.platform == "darwin":
        result = subprocess.run(
            [
                "/usr/bin/ditto",
                "-c",
                "-k",
                "--keepParent",
                str(target),
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3600,
        )
        if result.returncode != 0:
            raise RecoveryError((result.stderr or result.stdout or "ditto failed").strip()[-400:])
        return
    # A real .zip temp plus an atomic rename: PowerShell's Compress-Archive
    # rejects the temporary extension and omits hidden files, so the standard
    # library writes the archive instead.
    base_name = destination.with_name(f".{destination.stem}.build")
    created = shutil.make_archive(
        str(base_name), "zip", root_dir=str(target.parent), base_dir=target.name
    )
    os.replace(created, destination)


def recovery_can_offer(failure: object) -> bool:
    """Return True when this failure can be repaired from a valid local cache."""

    if not _recovery_is_frozen() or not _recovery_is_packaged_tools_failure(failure):
        return False
    valid, _detail, _metadata = _recovery_cache_status()
    return valid


def recovery_start(*, relaunch: bool = True) -> dict[str, Any]:
    """Install the cached copy through a detached helper after verification."""

    operation_id = _recovery_operation_id()
    result_path = _recovery_result_path(operation_id)
    if not _recovery_is_frozen():
        return {
            "ok": False,
            "detail": "Recovery is only available in the packaged application. "
            "Start the app with run_macos.command or run_windows.bat instead.",
            "result_path": str(result_path),
        }
    target = _recovery_target_path()
    if target is None or not target.is_dir():
        return {
            "ok": False,
            "detail": "The installed application could not be located.",
            "result_path": str(result_path),
        }
    manifest = _recovery_read_manifest(target)
    if manifest is None:
        return {
            "ok": False,
            "detail": "The installed application manifest is unreadable.",
            "result_path": str(result_path),
        }
    matches, identity_detail = _recovery_identity_matches(manifest)
    if not matches:
        return {
            "ok": False,
            "detail": f"The installed application is not the expected build: {identity_detail}.",
            "result_path": str(result_path),
        }
    try:
        backup = _recovery_backup_path(target)
    except RecoveryError as error:
        return {
            "ok": False,
            "detail": str(error),
            "result_path": str(result_path),
            "target_path": str(target),
        }
    valid, detail, _metadata = _recovery_cache_status()
    if not valid:
        return {
            "ok": False,
            "detail": f"The recovery cache cannot be used: {detail}.",
            "result_path": str(result_path),
        }
    archive = _recovery_archive_path()
    audit_ok, audit_detail, _total = _recovery_audit_archive(archive)
    if not audit_ok:
        return {
            "ok": False,
            "detail": f"The recovery archive was rejected: {audit_detail}.",
            "result_path": str(result_path),
        }

    locked, lock_detail = _recovery_acquire_lock(operation_id)
    if not locked:
        return {
            "ok": False,
            "detail": lock_detail,
            "result_path": str(result_path),
            "target_path": str(target),
        }

    # A fresh unique directory on the same volume. A predictable name is never
    # removed, because it may belong to something else entirely.
    container = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.recovery-", dir=str(target.parent))
    )
    try:
        _recovery_extract_archive(archive, container)
        bundle_root = _recovery_find_bundle_root(container)
        if bundle_root is None:
            raise RecoveryError("the staged copy has no application bundle")
        verified, verify_detail = _recovery_verify_staged(bundle_root)
        if not verified:
            raise RecoveryError(verify_detail)
    except (OSError, RecoveryError, zipfile.BadZipFile) as error:
        shutil.rmtree(container, ignore_errors=True)
        _recovery_release_lock()
        return {
            "ok": False,
            "detail": f"The repair was stopped before touching the application: {error}.",
            "result_path": str(result_path),
            "target_path": str(target),
            "backup_path": str(backup),
        }

    helper_directory = Path(tempfile.mkdtemp(prefix="mediatovideo-recovery-helper-"))
    helper = _recovery_write_helper(helper_directory, relaunch)
    result_path.unlink(missing_ok=True)
    staged_executable = _recovery_staged_executable(bundle_root)
    relaunch_command = (
        str(target / staged_executable.name)
        if staged_executable is not None
        else str(target)
    )
    try:
        helper_pid = _recovery_launch_helper(
            helper,
            _recovery_helper_arguments(
                target=target,
                candidate=bundle_root,
                backup=backup,
                result_path=result_path,
                lock_path=_recovery_lock_path(),
                relaunch=relaunch,
                relaunch_command=relaunch_command,
            ),
        )
    except OSError as error:
        shutil.rmtree(container, ignore_errors=True)
        _recovery_release_lock()
        return {
            "ok": False,
            "detail": f"The repair helper could not be started: {error}.",
            "result_path": str(result_path),
            "target_path": str(target),
            "backup_path": str(backup),
        }
    _recovery_handover_lock(helper_pid, operation_id)
    _recovery_log(
        "Recovery repair started",
        target=str(target),
        relaunch=relaunch,
        result_path=str(result_path),
    )
    return {
        "ok": True,
        "detail": "The repair helper is running; the application will restart when it finishes.",
        "result_path": str(result_path),
        "target_path": str(target),
        "backup_path": str(backup),
    }


def _recovery_helper_arguments(
    *,
    target: Path,
    candidate: Path,
    backup: Path,
    result_path: Path,
    lock_path: Path,
    relaunch: bool,
    relaunch_command: str,
) -> list[str]:
    """Return the helper's raw argv values.

    Nothing here is shell-quoted: ``Popen`` receives a list, so a quoted string
    would be delivered as a path that literally contains quote characters.
    """

    log_path = ""
    try:
        from . import diagnostics

        path = diagnostics.diagnostics_log_path()
        log_path = str(path) if path else ""
    except Exception:  # noqa: BLE001 - a missing log path is not fatal
        log_path = ""
    values = [
        str(os.getpid()),
        str(candidate),
        str(target),
        str(backup),
        log_path,
        str(result_path),
        str(lock_path),
        str(RECOVERY_HELPER_WAIT_SECONDS),
    ]
    if sys.platform == "darwin":
        return [*values, "yes" if relaunch else "no", "/usr/bin/open", "-n", str(target)]
    return [*values, "1" if relaunch else "0", relaunch_command]


_POSIX_HELPER = """#!/bin/sh
# Swap a verified recovery candidate into place after this application exits.
# Arguments are raw argv values handed to /bin/sh by Popen: this script never
# evaluates a path, and an explicit PATH keeps the helper usable when the
# application was started with an empty environment.
set -u
PATH=/usr/bin:/bin
export PATH
parent_pid="$1"
candidate="$2"
target="$3"
backup="$4"
log_path="$5"
result_path="$6"
lock_path="$7"
wait_seconds="$8"
relaunch="$9"
shift 9

log_result() {
    if [ -n "$log_path" ]; then
        printf '%s\n' "$1" >> "$log_path" 2>/dev/null
    fi
}

release_lock() {
    if [ -z "$lock_path" ] || [ ! -f "$lock_path" ]; then
        return 0
    fi
    owner=$(/usr/bin/head -n 1 "$lock_path" 2>/dev/null)
    if [ "$owner" = "pid=$$" ]; then
        /bin/rm -f "$lock_path" 2>/dev/null
    fi
}

write_result() {
    if [ -n "$result_path" ]; then
        temporary="$result_path.tmp.$$"
        printf '{"ok":%s,"detail":"%s","target_path":%s,"backup_path":%s}\n' "$1" "$2" "$RECOVERY_RESULT_TARGET_JSON" "$RECOVERY_RESULT_BACKUP_JSON" > "$temporary" 2>/dev/null
        /bin/mv -f "$temporary" "$result_path" 2>/dev/null
    fi
}

finish() {
    release_lock
    write_result "$1" "$2"
}

deadline=$(( $(/bin/date +%s) + wait_seconds ))
while /bin/kill -0 "$parent_pid" 2>/dev/null; do
    if [ "$(/bin/date +%s)" -ge "$deadline" ]; then
        log_result "recovery: the application did not exit in time; nothing was changed."
        finish false "the application did not exit in time"
        exit 1
    fi
    /bin/sleep 1
done

if ! /bin/mv "$target" "$backup"; then
    log_result "recovery: the current application could not be moved aside; nothing was changed."
    finish false "the current application could not be moved aside"
    exit 1
fi

if ! /bin/mv "$candidate" "$target"; then
    if /bin/mv "$backup" "$target" 2>/dev/null; then
        log_result "recovery: the repair could not be installed; the original application was restored."
        finish false "the repair could not be installed and the original was restored"
    else
        log_result "recovery: the repair could not be installed and the original could not be restored automatically; it is kept at $backup"
        finish false "the repair could not be installed and the original was kept as the backup copy"
    fi
    exit 1
fi

/bin/rmdir "$(/usr/bin/dirname "$candidate")" 2>/dev/null
log_result "recovery: the repair was installed; the previous copy is kept at $backup"
finish true "repair installed"

if [ "$relaunch" = "yes" ]; then
    if ! "$@"; then
        log_result "recovery: the repaired application could not be relaunched."
        exit 1
    fi
fi
exit 0
"""


_POWERSHELL_HELPER = """param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$Candidate,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][string]$Backup,
    [string]$LogPath = "",
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [Parameter(Mandatory=$true)][string]$LockPath,
    [Parameter(Mandatory=$true)][int]$WaitSeconds,
    [string]$Relaunch = "1",
    [string]$RelaunchCommand = ""
)
$ErrorActionPreference = "Stop"

function Write-Log([string]$Message) {
    if ($LogPath -ne "") { Add-Content -LiteralPath $LogPath -Value $Message -ErrorAction SilentlyContinue }
}
function Release-Lock {
    if ($LockPath -eq "") { return }
    try { $first = Get-Content -LiteralPath $LockPath -TotalCount 1 } catch { return }
    if ("$first" -eq "pid=$PID") { Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue }
}
function Write-Result([bool]$Ok, [string]$Detail) {
    $payload = '{"ok":' + $Ok.ToString().ToLower() + ',"detail":"' + $Detail + '","target_path":' + $env:RECOVERY_RESULT_TARGET_JSON + ',"backup_path":' + $env:RECOVERY_RESULT_BACKUP_JSON + '}'
    $temporary = $ResultPath + ".tmp"
    [System.IO.File]::WriteAllText($temporary, $payload, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $ResultPath -Force
}
function Finish([bool]$Ok, [string]$Detail) { Release-Lock; Write-Result $Ok $Detail }
function Show-NativeResult([string]$Detail) {
    if ($Relaunch -ne "1") { return }
    try { (New-Object -ComObject WScript.Shell).Popup($Detail, 0, "Mediatovideo Converter", 16) | Out-Null } catch { }
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
    if ((Get-Date) -gt $deadline) {
        Write-Log "recovery: the application did not exit in time; nothing was changed."
        Finish $false "the application did not exit in time"
        Show-NativeResult "The repair stopped: the application is still running."
        exit 1
    }
    Start-Sleep -Seconds 1
}

try { Move-Item -LiteralPath $Target -Destination $Backup -ErrorAction Stop }
catch {
    Write-Log "recovery: the current application could not be moved aside; nothing was changed."
    Finish $false "the current application could not be moved aside"
    Show-NativeResult "The repair could not move the current application aside."
    exit 1
}

try { Move-Item -LiteralPath $Candidate -Destination $Target -ErrorAction Stop }
catch {
    $restored = $false
    try { Move-Item -LiteralPath $Backup -Destination $Target -ErrorAction Stop; $restored = $true } catch { }
    if ($restored) {
        Write-Log "recovery: the repair could not be installed; the original application was restored."
        Finish $false "the repair could not be installed and the original was restored"
    } else {
        Write-Log "recovery: the repair could not be installed and the original could not be restored automatically; it is kept at $Backup"
        Finish $false "the repair could not be installed and the original was kept as the backup copy"
    }
    Show-NativeResult "The repair failed. Check the diagnostic log for details."
    exit 1
}

try {
    $container = Split-Path -Parent $Candidate
    if ($container -ne "") { Remove-Item -LiteralPath $container -Force -Recurse -ErrorAction SilentlyContinue }
} catch { }
Write-Log "recovery: the repair was installed; the previous copy is kept at $Backup"
Finish $true "repair installed"

if ($Relaunch -eq "1") {
    try { Start-Process -FilePath $RelaunchCommand -ErrorAction Stop }
    catch {
        Write-Log "recovery: the repaired application could not be relaunched."
        Show-NativeResult "The repair was installed but the application could not start."
        exit 1
    }
}
exit 0
"""
