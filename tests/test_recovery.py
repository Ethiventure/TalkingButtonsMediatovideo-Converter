"""Isolated tests for the local recovery cache, archive audit and swap helper.

Recovery only runs in a packaged application, so the tests fake the frozen
state and the installed target, exercise the real cache, archive-audit and
helper code, and never import the runtime module.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mediatovideo_converter import recovery

REAL_CACHE_DIRECTORY = recovery._recovery_cache_directory

IS_MACOS = sys.platform == "darwin"
TOOL_SUFFIX = ".exe" if sys.platform == "win32" else ""
BUNDLE_DIRECTORY_NAME = (
    "Mediatovideo Converter.app" if IS_MACOS else "Mediatovideo Converter"
)

CANONICAL_FAILURE = (
    "Stage: Checking packaged video tools\n\n"
    "Problem: The packaged FFmpeg and FFprobe tools are missing or incomplete.\n"
    "What to do: Install the application again from a fresh download."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecoveryTestCase(unittest.TestCase):
    """Base case with a private cache directory and a fake installed bundle."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatovideo-recovery-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.patch(mock.patch.object(recovery, "_recovery_cache_directory", lambda: self.cache))
        self.patch(mock.patch.object(recovery, "_recovery_is_frozen", lambda: True))
        self.app = self.make_bundle(self.root / BUNDLE_DIRECTORY_NAME)
        self.patch(mock.patch.object(recovery, "_recovery_target_path", lambda: self.app))
        self.patch(mock.patch.object(recovery, "_recovery_codesign_verify", lambda _root: (True, "signed")))

    def patch(self, patcher: mock._patch) -> mock.Mock:
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def make_bundle(self, app: Path, *, ffmpeg_bytes: bytes = b"ffmpeg-binary") -> Path:
        """Create a bundle directory with a manifest and video tools."""

        if IS_MACOS:
            tools = app / "Contents" / "Frameworks" / "video_tools"
            manifest_path = app / "Contents" / "Resources" / "BUILD-MANIFEST.json"
            executable = app / "Contents" / "MacOS" / "Mediatovideo Converter"
        else:
            # The module's non-macOS layout: manifest at the bundle root, the
            # tools under _internal, and a root-level executable.
            tools = app / "_internal" / "video_tools"
            manifest_path = app / "BUILD-MANIFEST.json"
            executable = app / "Mediatovideo Converter.exe"
        tools.mkdir(parents=True)
        ffmpeg_file = tools / f"ffmpeg{TOOL_SUFFIX}"
        ffprobe_file = tools / f"ffprobe{TOOL_SUFFIX}"
        ffmpeg_file.write_bytes(ffmpeg_bytes)
        ffprobe_file.write_bytes(b"ffprobe-binary")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"exe")
        # This is the real build manifest shape: packaged digests live in
        # "hashes" and the "tools" object carries the version report.
        manifest = {
            "application": {"name": "Mediatovideo Converter", "version": recovery.__version__},
            "build": {"platform": sys.platform, "machine": recovery.platform.machine()},
            "hashes": {
                "ffmpeg": sha256(ffmpeg_file),
                "ffprobe": sha256(ffprobe_file),
            },
            "tools": {
                "ffmpeg": {"version": "8.1.2", "banner": "ffmpeg version 8.1.2"},
                "ffprobe": {"version": "8.1.2"},
            },
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return app

    def tools_directory(self) -> Path:
        """Return the video-tools folder for this platform's bundle layout."""
        if IS_MACOS:
            return self.app / "Contents" / "Frameworks" / "video_tools"
        return self.app / "_internal" / "video_tools"

    def manifest_file(self) -> Path:
        """Return the manifest path for this platform's bundle layout."""
        if IS_MACOS:
            return self.app / "Contents" / "Resources" / "BUILD-MANIFEST.json"
        return self.app / "BUILD-MANIFEST.json"

    def archive_manifest_member(self) -> str:
        """Return the in-archive manifest path for this platform's layout."""
        prefix = (
            "Mediatovideo Converter.app/Contents/Resources"
            if IS_MACOS
            else "Mediatovideo Converter"
        )
        return f"{prefix}/BUILD-MANIFEST.json"

    def seed_cache(self, *, archive_name: str = "mediatovideo-recovery.zip") -> Path:
        """Write a valid archive and matching metadata into the test cache."""

        archive = self.cache / archive_name
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(self.archive_manifest_member(), "{}")
        metadata = {
            "schema": recovery.RECOVERY_METADATA_SCHEMA,
            **recovery._recovery_identity(),
            "archive": archive.name,
            "archive_sha256": sha256(archive),
            "archive_bytes": archive.stat().st_size,
            "uncompressed_bytes": archive.stat().st_size,
        }
        (self.cache / recovery.RECOVERY_METADATA_NAME).write_text(json.dumps(metadata), encoding="utf-8")
        return archive


class CacheValidationTests(RecoveryTestCase):
    """Cache metadata, digest and identity are all enforced."""

    def test_cache_directory_is_keyed_by_version_platform_and_machine(self) -> None:
        support = self.root / "support"
        self.patch(mock.patch.object(recovery, "_recovery_cache_directory", REAL_CACHE_DIRECTORY))
        self.patch(mock.patch.object(recovery, "_recovery_app_support_root", lambda: support))

        directory = recovery._recovery_cache_directory()

        identity = recovery._recovery_identity()
        self.assertEqual(
            directory,
            support
            / recovery.APPLICATION_DIRECTORY_NAME
            / recovery.RECOVERY_CACHE_DIRECTORY_NAME
            / f"{identity['version']}-{identity['platform']}-{identity['machine']}",
        )
        self.patch(mock.patch.object(recovery, "__version__", "9.9.9"))
        self.assertNotEqual(recovery._recovery_cache_directory(), directory)

    def test_tool_hashes_come_from_the_manifest_hashes_field(self) -> None:
        manifest = recovery._recovery_read_manifest(self.app)
        self.assertIsNotNone(manifest)
        self.assertNotIn(
            "sha256", manifest["tools"]["ffmpeg"]  # type: ignore[index]
        )

        matches, detail = recovery._recovery_tool_hashes(
            manifest, recovery._recovery_tools_root(self.app)  # type: ignore[arg-type]
        )

        self.assertTrue(matches, detail)
        (self.tools_directory() / f"ffmpeg{TOOL_SUFFIX}").write_bytes(b"tampered")
        matches, _detail = recovery._recovery_tool_hashes(
            manifest, recovery._recovery_tools_root(self.app)  # type: ignore[arg-type]
        )
        self.assertFalse(matches)

    def test_valid_cache_matches_identity_and_digest(self) -> None:
        self.seed_cache()

        valid, detail, metadata = recovery._recovery_cache_status()

        self.assertTrue(valid, detail)
        self.assertEqual(metadata["version"], recovery.__version__)

    def test_corrupted_archive_digest_is_rejected(self) -> None:
        archive = self.seed_cache()
        archive.write_bytes(archive.read_bytes() + b"corruption")

        valid, detail, _metadata = recovery._recovery_cache_status()

        self.assertFalse(valid)
        self.assertIn("digest", detail)

    def test_version_and_machine_mismatch_is_rejected(self) -> None:
        self.seed_cache()
        metadata_path = self.cache / recovery.RECOVERY_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["machine"] = "a-different-machine"
        metadata_path.write_text(json.dumps(payload), encoding="utf-8")

        valid, detail, _metadata = recovery._recovery_cache_status()

        self.assertFalse(valid)
        self.assertIn("machine", detail)


class ArchiveAuditTests(RecoveryTestCase):
    """Unsafe archives are rejected before anything is extracted."""

    def audit(self, name: str, writer: object) -> tuple[bool, str]:
        archive = self.cache / name
        with zipfile.ZipFile(archive, "w") as handle:
            writer(handle)  # type: ignore[operator]
        ok, detail, _total = recovery._recovery_audit_archive(archive)
        return ok, detail

    def test_safe_archive_passes(self) -> None:
        ok, detail = self.audit(
            "safe.zip", lambda h: h.writestr("App.app/Contents/Info.plist", "data")
        )

        self.assertTrue(ok, detail)

    def test_traversal_absolute_drive_and_backslash_are_rejected(self) -> None:
        cases = {
            "traversal": "../evil",
            "absolute": "/etc/passwd",
            "drive": "C:/evil",
            "backslash": "App.app\\evil",
        }
        for label, name in cases.items():
            with self.subTest(label=label):
                def writer(handle: zipfile.ZipFile, raw: str = name) -> None:
                    if "\\" in raw:
                        # Force the raw central-directory name: ZipInfo would
                        # otherwise normalise a backslash on Windows.
                        info = zipfile.ZipInfo("placeholder")
                        info.filename = raw
                        info.orig_filename = raw
                        handle.writestr(info, "x")
                    else:
                        handle.writestr(raw, "x")

                ok, _detail = self.audit(f"{label}.zip", writer)
                self.assertFalse(ok)

    def test_duplicate_entries_are_rejected(self) -> None:
        def writer(handle: zipfile.ZipFile) -> None:
            handle.writestr("App.app/file", "one")
            handle.writestr("App.app/file", "two")

        ok, detail = self.audit("duplicate.zip", writer)

        self.assertFalse(ok)
        self.assertIn("repeats", detail)

    def test_raw_backslash_entry_is_rejected_when_the_reader_normalises_names(self) -> None:
        def writer(handle: zipfile.ZipFile) -> None:
            info = zipfile.ZipInfo("placeholder")
            info.filename = "App.app\\evil"
            info.orig_filename = "App.app\\evil"
            handle.writestr(info, "x")

        archive = self.cache / "raw-backslash.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            writer(handle)
        # Emulate the Windows reader, which rewrites os.sep in filename while
        # orig_filename keeps the raw central-directory name.
        with mock.patch.object(recovery.zipfile.os, "sep", "\\"):
            with zipfile.ZipFile(archive) as handle:
                member = handle.infolist()[0]
                self.assertNotIn("\\", member.filename)
                self.assertIn("\\", member.orig_filename)
            ok, detail, _total = recovery._recovery_audit_archive(archive)

        self.assertFalse(ok)
        self.assertIn("backslash", detail)

    def test_nul_in_a_raw_entry_name_is_rejected(self) -> None:
        class FakeMember:
            filename = "App.app/ok"
            orig_filename = "App.app/ok\x00evil"
            external_attr = 0
            file_size = 0
            compress_size = 0

        class FakeArchive:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeArchive":
                return self
            def __exit__(self, *_args: object) -> None:
                return None
            def infolist(self) -> list:
                return [FakeMember()]
            def read(self, _member: object) -> bytes:
                return b""

        placeholder = self.cache / "nul.zip"
        placeholder.write_bytes(b"PK\\x05\\x06" + b"\\x00" * 18)
        with mock.patch.object(recovery.zipfile, "ZipFile", FakeArchive):
            ok, detail, _total = recovery._recovery_audit_archive(placeholder)

        self.assertFalse(ok)
        self.assertIn("NUL", detail)

    @unittest.skipIf(sys.platform == "win32", "symlinks are rejected wholesale on Windows")
    def test_symlink_escape_and_symlinked_ancestor_are_rejected(self) -> None:
        def escape(handle: zipfile.ZipFile) -> None:
            info = zipfile.ZipInfo("App.app/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            handle.writestr(info, "../../etc/passwd")

        ok, detail = self.audit("link-escape.zip", escape)
        self.assertFalse(ok)
        self.assertIn("outside its package", detail)

        def ancestor(handle: zipfile.ZipFile) -> None:
            link = zipfile.ZipInfo("App.app/frameworks")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            handle.writestr(link, "Contents")
            handle.writestr("App.app/frameworks/lib.dylib", "data")

        ok, detail = self.audit("link-ancestor.zip", ancestor)
        self.assertFalse(ok)
        self.assertIn("symlinked directory", detail)

    @unittest.skipIf(sys.platform == "win32", "symlinks are rejected wholesale on Windows")
    def test_internal_parent_symlink_is_allowed_in_any_order(self) -> None:
        def writer(handle: zipfile.ZipFile) -> None:
            # A real PyInstaller app links Resources/x -> ../../Frameworks/x,
            # and the file member can appear before or after the link.
            handle.writestr("App.app/Contents/Frameworks/data.bin", "payload")
            link = zipfile.ZipInfo("App.app/Contents/Resources/data.bin")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            handle.writestr(link, "../../Frameworks/data.bin")

        ok, detail = self.audit("internal-link.zip", writer)

        self.assertTrue(ok, detail)

    @unittest.skipIf(sys.platform == "win32", "symlinks are rejected wholesale on Windows")
    def test_prefix_attack_is_order_independent(self) -> None:
        def writer(handle: zipfile.ZipFile) -> None:
            # The file appears first, then the directory symlink that would
            # redirect it outside the package.
            handle.writestr("App.app/frameworks/lib.dylib", "data")
            link = zipfile.ZipInfo("App.app/frameworks")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            handle.writestr(link, "Contents")

        ok, detail = self.audit("prefix.zip", writer)

        self.assertFalse(ok)
        self.assertIn("symlinked directory", detail)

    def test_special_file_is_rejected(self) -> None:
        def writer(handle: zipfile.ZipFile) -> None:
            info = zipfile.ZipInfo("App.app/fifo")
            info.external_attr = (stat.S_IFIFO | 0o644) << 16
            handle.writestr(info, "")

        ok, detail = self.audit("fifo.zip", writer)

        self.assertFalse(ok)
        self.assertIn("special file", detail)

    def test_size_and_member_caps_are_enforced(self) -> None:
        with mock.patch.object(recovery, "RECOVERY_MAX_ARCHIVE_MEMBERS", 1):
            ok, detail = self.audit(
                "members.zip",
                lambda h: (h.writestr("a", "1"), h.writestr("b", "2")),
            )
        self.assertFalse(ok)
        self.assertIn("too many entries", detail)

        with mock.patch.object(recovery, "RECOVERY_MAX_UNCOMPRESSED_BYTES", 4):
            ok, detail = self.audit("size.zip", lambda h: h.writestr("big", "0123456789"))
        self.assertFalse(ok)
        self.assertIn("expands beyond", detail)


class CanOfferTests(RecoveryTestCase):
    """Only the canonical packaged-tools failure with a valid cache is offered."""

    def test_canonical_failure_text_with_valid_cache_is_offered(self) -> None:
        self.seed_cache()

        self.assertTrue(recovery.recovery_can_offer(CANONICAL_FAILURE))

    def test_generic_video_tools_stage_is_not_offered(self) -> None:
        self.seed_cache()

        self.assertFalse(
            recovery.recovery_can_offer(
                "Stage: Checking video tools\n\nProblem: FFmpeg was not found."
            )
        )
        self.assertFalse(recovery.recovery_can_offer("Stage: Scanning source"))

    def test_without_a_valid_cache_no_offer_is_made(self) -> None:
        self.assertFalse(recovery.recovery_can_offer(CANONICAL_FAILURE))

    def test_source_mode_never_offers_recovery(self) -> None:
        self.seed_cache()
        self.patch(mock.patch.object(recovery, "_recovery_is_frozen", lambda: False))

        self.assertFalse(recovery.recovery_can_offer(CANONICAL_FAILURE))
        seed = recovery.recovery_seed()
        started = recovery.recovery_start(relaunch=False)

        self.assertFalse(seed["ok"])
        self.assertIn("run_macos.command", seed["detail"])
        self.assertFalse(started["ok"])
        self.assertIn("result_path", started)

    def test_target_text_reports_the_running_bundle(self) -> None:
        self.assertEqual(recovery.recovery_target_text(), str(self.app))

        self.patch(mock.patch.object(recovery, "_recovery_target_path", lambda: None))
        self.assertEqual(recovery.recovery_target_text(), "")


class SeedTests(RecoveryTestCase):
    """Seeding verifies the source bundle and never overwrites a valid cache."""

    def test_seed_creates_a_valid_cache(self) -> None:
        result = recovery.recovery_seed()

        self.assertTrue(result["ok"], result)
        valid, detail, _metadata = recovery._recovery_cache_status()
        self.assertTrue(valid, detail)

    def test_damaged_app_does_not_replace_a_valid_cache(self) -> None:
        self.assertTrue(recovery.recovery_seed()["ok"])
        original_digest = recovery._recovery_cache_status()[2]["archive_sha256"]
        (self.tools_directory() / f"ffmpeg{TOOL_SUFFIX}").write_bytes(b"damaged")

        second = recovery.recovery_seed()

        self.assertTrue(second["ok"])
        self.assertEqual(recovery._recovery_cache_status()[2]["archive_sha256"], original_digest)

    def test_tool_hash_mismatch_refuses_to_seed(self) -> None:
        (self.tools_directory() / f"ffmpeg{TOOL_SUFFIX}").write_bytes(b"damaged")

        result = recovery.recovery_seed()

        self.assertFalse(result["ok"])
        self.assertIn("video tools", result["detail"])
        self.assertFalse(recovery._recovery_cache_status()[0])

    def test_missing_manifest_refuses_to_seed(self) -> None:
        self.manifest_file().unlink()

        result = recovery.recovery_seed()

        self.assertFalse(result["ok"])
        self.assertIn("manifest", result["detail"])


class StartTests(RecoveryTestCase):

    def stage_report(self, payload: dict, returncode: int = 0) -> None:
        """Make the staged self-test write this report and exit with this code."""

        def run(command, **_kwargs):
            report = Path(command[command.index("--self-test-report") + 1])
            report.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, returncode, "", "")

        self.patch(mock.patch.object(recovery.subprocess, "run", run))

    def good_report(self) -> dict:
        return {
            "ok": True,
            "frozen": True,
            "path": "",
            "gui": {"tk": "9.0.4"},
            "video": {"tools": {"ffmpeg": {"version": "8.1.2"}}},
            "diagnostics": {"startup_logged": True},
        }

    def test_self_test_requires_clean_exit_and_a_full_report(self) -> None:
        self.stage_report(self.good_report())
        self.assertEqual(recovery._recovery_run_self_test(self.app)[0], True)

        self.stage_report(self.good_report(), returncode=1)
        self.assertFalse(recovery._recovery_run_self_test(self.app)[0])

    def test_self_test_rejects_a_partial_or_forged_report(self) -> None:
        cases = {
            "not frozen": {"frozen": False},
            "path inherited": {"path": "/usr/bin:/bin"},
            "no gui result": {"gui": {}},
            "no diagnostics result": {"diagnostics": {}},
        }
        for label, override in cases.items():
            with self.subTest(label=label):
                payload = self.good_report()
                payload.update(override)
                self.stage_report(payload)
                ok, detail = recovery._recovery_run_self_test(self.app)
                self.assertFalse(ok, label)
                self.assertTrue(detail)

    """Staging is verified before anything can replace the installed app."""

    def test_start_stages_and_starts_a_detached_helper(self) -> None:
        self.seed_cache()
        launched: dict[str, object] = {}
        self.patch(mock.patch.object(recovery, "_recovery_verify_staged", lambda _root: (True, "ok")))
        self.patch(
            mock.patch.object(
                recovery,
                "_recovery_launch_helper",
                lambda helper, args: launched.update(helper=helper, args=args) or 4321,
            )
        )

        result = recovery.recovery_start(relaunch=False)

        self.assertTrue(result["ok"], result)
        result_path = Path(result["result_path"])
        self.assertEqual(result_path.parent, self.cache)
        self.assertTrue(result_path.name.startswith("recovery-result-"))
        self.assertEqual(result["target_path"], str(self.app))
        self.assertTrue(result["backup_path"].startswith(str(self.app) + ".previous-"))
        arguments = launched["args"]
        # Raw argv: no quotes anywhere, the candidate is the staged bundle
        # itself (not the container), and no trailing quote characters leak in.
        if IS_MACOS:
            self.assertIn("no", arguments)
            self.assertIn("/usr/bin/open", arguments)
        else:
            self.assertIn("0", arguments)
        self.assertIn(str(self.app), arguments)
        for value in arguments:
            self.assertNotIn("'", value)
        self.assertNotIn(str(self.root / f".{self.app.name}.recovery-{os.getpid()}"), arguments)
        # The lock now belongs to the helper, not to this process.
        lock_text = (self.cache / "repair.lock").read_text(encoding="utf-8")
        self.assertEqual(lock_text.splitlines()[0], "pid=4321")

    def test_second_start_is_refused_while_another_repair_holds_the_lock(self) -> None:
        self.seed_cache()
        self.patch(mock.patch.object(recovery, "_recovery_verify_staged", lambda _root: (True, "ok")))
        self.patch(mock.patch.object(recovery, "_recovery_launch_helper", lambda *a: os.getpid()))
        self.assertTrue(recovery.recovery_start(relaunch=False)["ok"])

        second = recovery.recovery_start(relaunch=False)

        self.assertFalse(second["ok"])
        self.assertIn("already in progress", second["detail"])

    def test_stale_lock_from_a_dead_process_is_cleared(self) -> None:
        self.seed_cache()
        self.patch(mock.patch.object(recovery, "_recovery_verify_staged", lambda _root: (True, "ok")))
        self.patch(mock.patch.object(recovery, "_recovery_launch_helper", lambda *a: os.getpid()))
        (self.cache / "repair.lock").write_text(
            "pid=2147483646\noperation=stale\n", encoding="utf-8"
        )

        result = recovery.recovery_start(relaunch=False)

        self.assertTrue(result["ok"], result)
        self.assertIn(str(os.getpid()), (self.cache / "repair.lock").read_text(encoding="utf-8"))

    def test_handover_lock_is_swapped_in_atomically(self) -> None:
        lock = self.cache / "repair.lock"
        replaced: list[tuple[str, str]] = []
        real_replace = os.replace

        def replace(source: object, destination: object) -> None:
            replaced.append((str(source), str(destination)))
            real_replace(source, destination)  # type: ignore[arg-type]

        self.patch(mock.patch.object(recovery.os, "replace", replace))

        recovery._recovery_handover_lock(4321, "operation-1")

        self.assertEqual(len(replaced), 1)
        temporary, destination = replaced[0]
        self.assertEqual(destination, str(lock))
        self.assertNotEqual(temporary, str(lock))
        self.assertEqual(
            lock.read_text(encoding="utf-8").splitlines()[0], "pid=4321"
        )
        leftovers = [item.name for item in self.cache.iterdir() if item.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_partially_written_lock_is_not_stolen_during_the_grace_window(self) -> None:
        lock = self.cache / "repair.lock"
        lock.write_text("", encoding="utf-8")

        acquired, detail = recovery._recovery_acquire_lock("operation-2")

        self.assertFalse(acquired)
        self.assertIn("already in progress", detail)
        self.assertTrue(lock.exists())

    def test_unreadable_lock_is_cleared_once_the_grace_window_has_passed(self) -> None:
        lock = self.cache / "repair.lock"
        lock.write_text("", encoding="utf-8")
        stale = time.time() - recovery.RECOVERY_LOCK_GRACE_SECONDS - 5
        os.utime(lock, (stale, stale))

        acquired, _detail = recovery._recovery_acquire_lock("operation-3")

        self.assertTrue(acquired)
        self.assertEqual(
            lock.read_text(encoding="utf-8").splitlines()[0], f"pid={os.getpid()}"
        )

    def test_result_paths_are_unique_per_operation(self) -> None:
        first = recovery._recovery_result_path(recovery._recovery_operation_id())
        second = recovery._recovery_result_path(recovery._recovery_operation_id())

        self.assertNotEqual(first, second)
        self.assertFalse(first.exists() or second.exists())

    def test_preexisting_similarly_named_folder_is_left_alone(self) -> None:
        self.seed_cache()
        stale = self.app.with_name(f".{self.app.name}.recovery-{os.getpid()}")
        stale.mkdir()
        (stale / "keep.txt").write_text("keep", encoding="utf-8")
        self.patch(mock.patch.object(recovery, "_recovery_verify_staged", lambda _root: (True, "ok")))
        self.patch(
            mock.patch.object(recovery, "_recovery_launch_helper", lambda *a: os.getpid())
        )

        result = recovery.recovery_start(relaunch=False)

        self.assertTrue(result["ok"], result)
        self.assertTrue((stale / "keep.txt").is_file())
        containers = [
            path
            for path in self.root.iterdir()
            if path.name.startswith(f".{self.app.name}.recovery-")
        ]
        self.assertIn(stale, containers)

    def test_staging_or_self_test_failure_leaves_the_target_unchanged(self) -> None:
        self.seed_cache()
        before = sorted(str(path.relative_to(self.app)) for path in self.app.rglob("*"))
        self.patch(
            mock.patch.object(recovery, "_recovery_verify_staged", lambda _root: (False, "self-test failed"))
        )
        started: list[object] = []
        self.patch(mock.patch.object(recovery, "_recovery_launch_helper", lambda *a: started.append(a)))

        result = recovery.recovery_start(relaunch=False)

        self.assertFalse(result["ok"])
        self.assertIn("self-test failed", result["detail"])
        self.assertEqual(sorted(str(path.relative_to(self.app)) for path in self.app.rglob("*")), before)
        self.assertEqual(started, [])
        self.assertFalse((self.cache / "repair.lock").exists())

    def test_backup_names_are_unique_and_unused(self) -> None:
        first = recovery._recovery_backup_path(self.app)
        second = recovery._recovery_backup_path(self.app)

        self.assertNotEqual(first, second)
        self.assertFalse(first.exists() or second.exists())
        self.assertTrue(first.name.startswith(self.app.name + ".previous-"))


class SwapHelperTests(RecoveryTestCase):
    """The generated shell helper performs the swap, rollback and deadline."""

    def write_posix_helper(self, directory: Path) -> Path:
        """Write the POSIX helper directly so Ubuntu runs the same script."""
        directory.mkdir(parents=True, exist_ok=True)
        helper = directory / "mediatovideo-repair.sh"
        helper.write_text(recovery._POSIX_HELPER, encoding="utf-8")
        helper.chmod(0o700)
        return helper

    def run_helper(
        self,
        candidate: Path,
        target: Path,
        backup: Path,
        *,
        parent_pid: int = 2147483646,
        wait_seconds: int = 30,
        lock: Path | None = None,
        relaunch: str = "no",
    ) -> tuple[int, Path, Path, Path]:
        helper = self.write_posix_helper(self.root / "helper")
        log = self.root / "helper.log"
        result = self.root / "helper-result.json"
        lock_path = lock or (self.root / "repair.lock")
        # Raw argv, exactly as _recovery_launch_helper passes it to Popen.
        arguments = [
            str(parent_pid),
            str(candidate),
            str(target),
            str(backup),
            str(log),
            str(result),
            str(lock_path),
            str(wait_seconds),
            relaunch,
        ]
        environment = dict(os.environ)
        environment["RECOVERY_RESULT_TARGET_JSON"] = json.dumps(str(target))
        environment["RECOVERY_RESULT_BACKUP_JSON"] = json.dumps(str(backup))
        completed = subprocess.run(
            ["/bin/sh", str(helper), *arguments],
            check=False,
            timeout=120,
            env=environment,
        )
        return completed.returncode, log, result, lock_path

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_helper_installs_the_candidate_and_preserves_the_backup(self) -> None:
        helper_dir = self.root / "helper"
        helper_dir.mkdir()
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        backup = target.with_name("target.previous-1")

        code, log, result, _lock = self.run_helper(candidate, target, backup)

        self.assertEqual(code, 0)
        self.assertTrue((target / "new.txt").is_file())
        self.assertTrue((backup / "old.txt").is_file())
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["target_path"], str(target))
        self.assertEqual(payload["backup_path"], str(backup))
        self.assertIn("repair was installed", log.read_text(encoding="utf-8"))

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_helper_result_survives_quoted_paths(self) -> None:
        target = self.root / 'target "quoted"'
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        backup = target.with_name('target "quoted".previous')

        code, _log, result, _lock = self.run_helper(candidate, target, backup)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["backup_path"], str(backup))

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_rollback_result_is_valid_with_a_quoted_backup_path(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        backup = target.with_name('backup with "quotes" and \\ backslash')

        code, log, result, _lock = self.run_helper(
            self.root / "missing-candidate", target, backup
        )

        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["backup_path"], str(backup))
        self.assertIn("the original was restored", payload["detail"])
        self.assertIn("restored", log.read_text(encoding="utf-8"))

    @unittest.skipUnless(IS_MACOS, "macOS writes the POSIX helper")
    def test_write_helper_selects_the_posix_script_on_macos(self) -> None:
        helper = recovery._recovery_write_helper(self.root / "helper", relaunch=False)
        self.assertTrue(helper.name.endswith(".sh"))

    @unittest.skipUnless(sys.platform == "win32", "Windows writes the PowerShell helper")
    def test_write_helper_selects_the_powershell_script_on_windows(self) -> None:
        helper = recovery._recovery_write_helper(self.root / "helper", relaunch=False)
        self.assertTrue(helper.name.endswith(".ps1"))

    def test_result_detail_never_interpolates_a_path(self) -> None:
        # The JSON detail field is fixed text; only the escaped backup_path
        # field may carry the path, so a quote or backslash cannot break JSON.
        for script in (recovery._POSIX_HELPER, recovery._POWERSHELL_HELPER):
            for line in script.splitlines():
                stripped = line.strip()
                if stripped.startswith(("finish ", "Finish ")):
                    self.assertNotIn("$backup", stripped)
                    self.assertNotIn("$Backup", stripped)

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_helper_rolls_back_when_the_candidate_is_missing(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        backup = target.with_name("target.previous-2")

        code, log, result, _lock = self.run_helper(self.root / "missing-candidate", target, backup)

        self.assertEqual(code, 1)
        self.assertTrue((target / "old.txt").is_file())
        self.assertFalse(backup.exists())
        self.assertFalse(json.loads(result.read_text(encoding="utf-8"))["ok"])
        self.assertIn("original application was restored", log.read_text(encoding="utf-8"))

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_helper_gives_up_at_the_deadline_without_changing_anything(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        candidate = self.root / "candidate"
        candidate.mkdir()
        backup = target.with_name("target.previous-3")

        code, _log, result, _lock = self.run_helper(
            candidate, target, backup, parent_pid=os.getpid(), wait_seconds=1
        )

        self.assertEqual(code, 1)
        self.assertTrue((target / "old.txt").is_file())
        self.assertFalse(backup.exists())
        self.assertIn("did not exit in time", json.loads(result.read_text(encoding="utf-8"))["detail"])

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_repeated_repairs_each_keep_their_own_backup(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")

        for index in (1, 2):
            candidate = self.root / f"candidate-{index}"
            candidate.mkdir()
            (candidate / f"new-{index}.txt").write_text("new", encoding="utf-8")
            backup = recovery._recovery_backup_path(target)
            code, _log, _result, _lock = self.run_helper(candidate, target, backup)
            self.assertEqual(code, 0)
            self.assertTrue((backup / "old.txt").is_file() or (backup / f"new-{index - 1}.txt").is_file())

        backups = sorted(self.root.glob("target.previous-*"))
        self.assertEqual(len(backups), 2)

    def test_posix_helper_is_self_contained_and_ownership_safe(self) -> None:
        helper = self.write_posix_helper(self.root / "helper")
        text = helper.read_text(encoding="utf-8")

        self.assertIn("PATH=/usr/bin:/bin", text)
        self.assertIn('if [ "$relaunch" = "yes" ]', text)
        self.assertNotIn("eval_replacement", text)
        self.assertIn("release_lock", text)
        self.assertIn("/bin/mv -f \"$temporary\" \"$result_path\"", text)
        # The rich path stays in the plain-text log; the JSON detail is fixed.
        self.assertIn("it is kept at $backup", text)
        self.assertIn("the original was kept as the backup copy", text)

    def test_powershell_helper_only_removes_an_empty_staging_container(self) -> None:
        text = recovery._POWERSHELL_HELPER
        self.assertIn("[System.IO.Directory]::Delete($container, $false)", text)
        self.assertNotIn("Remove-Item -LiteralPath $container", text)

    def test_powershell_helper_writes_bom_free_json_and_rolls_back(self) -> None:
        text = recovery._POWERSHELL_HELPER

        self.assertIn("[System.IO.File]::WriteAllText($temporary, $payload", text)
        self.assertIn("[System.Text.UTF8Encoding]::new($false)", text)
        self.assertIn("Move-Item -LiteralPath $temporary -Destination $ResultPath -Force", text)
        self.assertIn("Release-Lock", text)
        self.assertNotIn("@{{", text)
        self.assertIn("Move-Item -LiteralPath $Backup -Destination $Target", text)

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_lock_is_released_only_by_its_owner(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        other_lock = self.root / "other.lock"
        other_lock.write_text(json.dumps({"pid": 999999}), encoding="utf-8")

        code, _log, _result, _lock = self.run_helper(
            candidate, target, target.with_name("target.previous-owner"), lock=other_lock
        )

        self.assertEqual(code, 0)
        # A lock owned by a different process must survive.
        self.assertTrue(other_lock.is_file())

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_owning_lock_is_removed_when_the_helper_finishes(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        lock = self.root / "owner.lock"
        helper = self.write_posix_helper(self.root / "helper")
        log = self.root / "own.log"
        result = self.root / "own.json"
        # exec keeps the shell's PID, so the lock written here is owned by the
        # helper itself and must be released by it.
        wrapper = self.root / "wrapper.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            'printf \'pid=%d\\n\' $$ > "$1"\n'
            "shift\n"
            'exec /bin/sh "$@"\n',
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["RECOVERY_RESULT_TARGET_JSON"] = json.dumps(str(target))
        environment["RECOVERY_RESULT_BACKUP_JSON"] = json.dumps(str(target.with_name("target.previous-owned")))
        completed = subprocess.run(
            [
                "/bin/sh",
                str(wrapper),
                str(lock),
                str(helper),
                "2147483646",
                str(candidate),
                str(target),
                str(target.with_name("target.previous-owned")),
                str(log),
                str(result),
                str(lock),
                "30",
                "no",
            ],
            check=False,
            timeout=120,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertFalse(lock.exists())
        self.assertTrue((target / "new.txt").is_file())

    @unittest.skipIf(sys.platform == "win32", "POSIX helper")
    def test_extracted_container_is_cleaned_up_after_a_successful_swap(self) -> None:
        helper_dir = self.root / "helper"
        helper_dir.mkdir(exist_ok=True)
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        container = self.root / "container"
        bundle = container / "App.app"
        bundle.mkdir(parents=True)
        (bundle / "new.txt").write_text("new", encoding="utf-8")

        code, _log, _result, _lock = self.run_helper(
            bundle, target, target.with_name("target.previous-container")
        )

        self.assertEqual(code, 0)
        self.assertFalse(container.exists())

    @unittest.skipUnless(sys.platform == "win32", "native Windows helper test")
    def test_windows_helper_removes_only_an_empty_staging_container(self) -> None:
        helper_dir = self.root / "helper"
        helper_dir.mkdir()
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        container = self.root / "container"
        bundle = container / "Mediatovideo Converter"
        bundle.mkdir(parents=True)
        (bundle / "new.txt").write_text("new", encoding="utf-8")
        backup = target.with_name("target.previous-container")
        result = self.root / "container-result.json"
        lock = self.root / "container.lock"
        lock.write_text("pid=999999\noperation=foreign\n", encoding="utf-8")
        helper = recovery._recovery_write_helper(helper_dir, relaunch=False)
        completed = subprocess.run(
            [
                str(recovery._recovery_windows_powershell()),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ParentPid",
                "2147483646",
                "-Candidate",
                str(bundle),
                "-Target",
                str(target),
                "-Backup",
                str(backup),
                "-LogPath",
                "",
                "-ResultPath",
                str(result),
                "-LockPath",
                str(lock),
                "-WaitSeconds",
                "30",
                "-Relaunch",
                "0",
                "-RelaunchCommand",
                str(target / "Mediatovideo Converter.exe"),
            ],
            check=False,
            timeout=120,
            env={
                **os.environ,
                "RECOVERY_RESULT_TARGET_JSON": json.dumps(str(target)),
                "RECOVERY_RESULT_BACKUP_JSON": json.dumps(str(backup)),
            },
        )

        self.assertEqual(completed.returncode, 0)
        self.assertFalse(container.exists())
        self.assertTrue((target / "new.txt").is_file())
        self.assertTrue((backup / "old.txt").is_file())
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["ok"], True)

    @unittest.skipUnless(sys.platform == "win32", "native Windows helper test")
    def test_windows_helper_keeps_siblings_in_a_shared_staging_folder(self) -> None:
        helper_dir = self.root / "helper"
        helper_dir.mkdir()
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        # A candidate staged directly in a shared folder: the parent also holds
        # the fixture's own result, backup and keep file, so it must survive.
        candidate = self.root / "shared-staging-candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        keep = self.root / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        backup = target.with_name("target.previous-shared")
        result = self.root / "shared-result.json"
        lock = self.root / "shared.lock"
        lock.write_text("pid=999999\noperation=foreign\n", encoding="utf-8")
        helper = recovery._recovery_write_helper(helper_dir, relaunch=False)
        completed = subprocess.run(
            [
                str(recovery._recovery_windows_powershell()),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ParentPid",
                "2147483646",
                "-Candidate",
                str(candidate),
                "-Target",
                str(target),
                "-Backup",
                str(backup),
                "-LogPath",
                "",
                "-ResultPath",
                str(result),
                "-LockPath",
                str(lock),
                "-WaitSeconds",
                "30",
                "-Relaunch",
                "0",
                "-RelaunchCommand",
                str(target / "Mediatovideo Converter.exe"),
            ],
            check=False,
            timeout=120,
            env={
                **os.environ,
                "RECOVERY_RESULT_TARGET_JSON": json.dumps(str(target)),
                "RECOVERY_RESULT_BACKUP_JSON": json.dumps(str(backup)),
            },
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(self.root.is_dir())
        self.assertTrue(keep.is_file())
        self.assertTrue(result.is_file())
        self.assertTrue(backup.is_dir())
        self.assertTrue(lock.is_file())
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["ok"], True)

    @unittest.skipUnless(sys.platform == "win32", "native Windows process query")
    def test_windows_pid_check_does_not_terminate_the_process(self) -> None:
        self.assertTrue(recovery._recovery_pid_alive(os.getpid()))
        self.assertFalse(recovery._recovery_pid_alive(2147483646))

    @unittest.skipUnless(sys.platform == "win32", "native Windows launch diagnosis")
    def test_windows_launch_diagnostic_matrix(self) -> None:
        """Record bounded controls for argv, environment and process flags."""
        full = dict(os.environ)
        minimal = {"PATH": "", "HOME": str(self.root), "USERPROFILE": str(self.root),
                   "LOCALAPPDATA": str(self.cache), "TEMP": str(self.root),
                   "TMP": str(self.root), "SystemRoot": os.environ["SystemRoot"]}
        names = ("ParentPid", "Candidate", "Target", "Backup", "LogPath",
                 "ResultPath", "LockPath", "WaitSeconds", "Relaunch", "RelaunchCommand")
        cases = [("minimal_os_path", "no_window", "raw", None),
                 ("full_empty_path", "no_window", "raw", None)]
        system = Path(os.environ["SystemRoot"])
        os_path = os.pathsep.join(str(p) for p in
                  (system / "System32", system, system / "System32" / "WindowsPowerShell" / "v1.0"))
        anchors = {}
        outcomes = []
        for index, (env_name, flags_name, shape, restored) in enumerate(cases):
            folder = self.root / f"matrix {index}'s fixture"
            folder.mkdir()
            target, candidate, backup = (folder / name for name in ("target", "candidate", "backup"))
            target.mkdir(); candidate.mkdir()
            result, lock = folder / "result.json", folder / "lock"
            helper = recovery._recovery_write_helper(folder, relaunch=False)
            arguments = ["2147483646", str(candidate), str(target), str(backup), "",
                         str(result), str(lock), "3", "0", str(target / "app.exe")]
            if shape == "marker":
                helper.write_text('param([string]$Marker)\n[System.IO.File]::WriteAllText($Marker, "ok")\n', encoding="utf-8")
                arguments = [str(result)]
            elif shape == "named":
                arguments = [value for pair in zip(("-" + name for name in names), arguments) for value in pair]
            environment = dict(full if env_name.startswith("full") else minimal)
            environment["PATH"] = "" if env_name == "full_empty_path" else os_path
            if restored:
                environment.update(anchors if restored == "all" else {restored: anchors[restored]})
            environment.update(RECOVERY_RESULT_TARGET_JSON=json.dumps(str(target)),
                               RECOVERY_RESULT_BACKUP_JSON=json.dumps(str(backup)))
            command = [str(recovery._recovery_windows_powershell()), "-NoProfile", "-ExecutionPolicy", "Bypass"]
            command += ["-File", str(helper), *arguments]
            log = folder / "startup.log"
            startupinfo = None
            if flags_name == "hidden_console":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags = subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            flags = {"regular": 0, "no_window": 0x08000200, "hidden_console": 0x10}[flags_name]
            with log.open("wb") as output:
                child = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=output,
                                         creationflags=flags, startupinfo=startupinfo)
                try:
                    status = child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    status = "timeout"
                    child.kill(); child.wait(timeout=5)
            outcomes.append(dict(env=env_name, flags=flags_name, shape=shape,
                                 restored=restored, status=status,
                                 result=result.is_file(), output=log.read_text(errors="replace")))
        print("WINDOWS_LAUNCH_MATRIX " + json.dumps(outcomes), flush=True)

    @unittest.skipUnless(sys.platform == "win32", "native detached Windows launcher")
    def test_windows_production_launcher_with_minimal_environment(self) -> None:
        """Exercise the actual raw argv and detached launch with hostile paths."""
        folder = self.root / "repair's test folder"
        folder.mkdir()
        target = folder / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        container = folder / "staging"
        container.mkdir()
        candidate = container / "candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        backup = folder / "target.previous"
        result = self.cache / "detached-result.json"
        lock = self.cache / "repair.lock"
        lock.write_text("pid=999999\noperation=foreign\n", encoding="utf-8")
        helper = recovery._recovery_write_helper(self.cache, relaunch=False)
        environment = {
            "PATH": "", "HOME": str(self.root), "USERPROFILE": str(self.root),
            "LOCALAPPDATA": str(self.cache), "TEMP": str(self.root),
            "TMP": str(self.root), "TMPDIR": str(self.root),
            "SystemRoot": os.environ["SystemRoot"],
        }
        arguments = ["2147483646", str(candidate), str(target), str(backup), "",
                     str(result), str(lock), "3", "0", str(target / "app.exe")]
        startup = result.with_suffix(".helper-startup.log")
        children = []
        real_popen = subprocess.Popen

        def cleanup():
            """Reap fixture children even when startup raises an exception."""
            for child in children:
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

        self.addCleanup(cleanup)

        def launch(*args, **kwargs):
            """Retain the fixture child so a failed test cannot orphan it."""
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        try:
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                recovery.subprocess, "Popen", side_effect=launch
            ):
                recovery._recovery_launch_helper(helper, arguments)
        except OSError as error:
            self.fail(f"{error}\n{startup.read_text(errors='replace')}")
        deadline = time.monotonic() + 20
        while not result.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(result.is_file(), startup.read_text(errors="replace"))
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["target_path"], str(target))
        self.assertTrue((target / "new.txt").is_file())
        self.assertTrue((backup / "old.txt").is_file())
        self.assertTrue(lock.is_file())

    @unittest.skipUnless(sys.platform == "win32", "native Windows helper test")
    def test_windows_helper_swaps_a_fake_directory(self) -> None:
        helper_dir = self.root / "helper"
        helper_dir.mkdir()
        target = self.root / "target"
        target.mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "new.txt").write_text("new", encoding="utf-8")
        backup = target.with_name("target.previous-win")
        result = self.root / "helper-result.json"
        lock = self.root / "repair.lock"
        lock.write_text("pid=999999\noperation=foreign\n", encoding="utf-8")
        helper = recovery._recovery_write_helper(helper_dir, relaunch=False)
        completed = subprocess.run(
            [
                str(recovery._recovery_windows_powershell()),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ParentPid",
                "2147483646",
                "-Candidate",
                str(candidate),
                "-Target",
                str(target),
                "-Backup",
                str(backup),
                "-LogPath",
                "",
                "-ResultPath",
                str(result),
                "-LockPath",
                str(lock),
                "-WaitSeconds",
                "30",
                "-Relaunch",
                "0",
                "-RelaunchCommand",
                str(target / "Mediatovideo Converter.exe"),
            ],
            check=False,
            timeout=120,
            env={
                **os.environ,
                "RECOVERY_RESULT_TARGET_JSON": json.dumps(str(target)),
                "RECOVERY_RESULT_BACKUP_JSON": json.dumps(str(backup)),
            },
        )
        self.assertEqual(completed.returncode, 0)
        self.assertTrue((target / "new.txt").is_file())
        self.assertTrue((backup / "old.txt").is_file())
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["target_path"], str(target))
        self.assertEqual(payload["backup_path"], str(backup))
        self.assertIn("\\", payload["backup_path"])
        # A lock owned by another process must survive the helper, and nothing
        # else in the fixture root may be removed either.
        self.assertEqual(lock.read_text(encoding="utf-8").splitlines()[0], "pid=999999")
        self.assertTrue(result.is_file())
        self.assertTrue(backup.is_dir())
        self.assertTrue(target.is_dir())
        self.assertTrue(result.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
