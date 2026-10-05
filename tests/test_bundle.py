"""Tests for bundled video-tool resolution, validation, and packaging."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediatovideo_converter import converter as converter_module
from mediatovideo_converter.converter import (
    FFmpegCompatibilityError,
    FFmpegNotFoundError,
    converter_find_tools,
    converter_minimum_versions,
    converter_verify_tools,
)
from mediatovideo_converter.models import (
    ConversionOptions,
    MediaGroup,
    NamingMode,
    OutputLayout,
    VideoFormat,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_app  # noqa: E402 - script import after sys.path adjustment


_TOOL_SUFFIX = ".exe" if os.name == "nt" else ""

def fake_tool_runner(
    version: str = "8.1.2",
    encoders: str = " V....D libx264  H.264 / AVC\n A....D aac     AAC\n",
    demuxers: str = " D  concat      Concatenated media files\n",
    configuration: str = "--enable-gpl --enable-version3",
):
    """Return a runner that answers like a supported FFmpeg build."""

    def run(command):
        if command[-1] == "-version":
            name = Path(command[0]).stem
            return 0, (
                f"{name} version {version} Copyright (c) 2000-2026\n"
                f"configuration: {configuration}\n"
            )
        if command[-1] == "-encoders":
            return 0, encoders
        if command[-1] == "-demuxers":
            return 0, demuxers
        return 1, ""

    return run


class ToolResolutionTests(unittest.TestCase):
    """Verify the resolution order owned by the converter module."""

    def setUp(self) -> None:
        converter_module._CONVERTER_TOOL_REPORT_CACHE.clear()

    def test_frozen_bundle_prefers_packaged_tools_over_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_tools = root / "bundle" / "video_tools"
            bundle_tools.mkdir(parents=True)
            for name in ("ffmpeg", "ffprobe"):
                (bundle_tools / f"{name}{_TOOL_SUFFIX}").write_bytes(b"binary")
            path_dir = root / "path"
            path_dir.mkdir()
            for name in ("ffmpeg", "ffprobe"):
                (path_dir / f"{name}{_TOOL_SUFFIX}").write_bytes(b"other")

            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "_MEIPASS", str(root / "bundle"), create=True
            ), mock.patch.dict(os.environ, {"PATH": str(path_dir)}):
                ffmpeg, ffprobe = converter_find_tools()

            self.assertEqual(ffmpeg, str(bundle_tools / f"ffmpeg{_TOOL_SUFFIX}"))
            self.assertEqual(ffprobe, str(bundle_tools / f"ffprobe{_TOOL_SUFFIX}"))

    def test_frozen_missing_pair_never_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bundle").mkdir()
            path_dir = root / "path"
            path_dir.mkdir()
            for name in ("ffmpeg", "ffprobe"):
                (path_dir / f"{name}{_TOOL_SUFFIX}").write_bytes(b"other")

            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "_MEIPASS", str(root / "bundle"), create=True
            ), mock.patch.dict(os.environ, {"PATH": str(path_dir)}):
                with self.assertRaises(FFmpegNotFoundError):
                    converter_find_tools()

    def test_frozen_without_bundle_root_never_uses_path(self) -> None:
        with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, '_MEIPASS', None, create=True), mock.patch('shutil.which') as which:
            with self.assertRaises(FFmpegNotFoundError):
                converter_find_tools()
            which.assert_not_called()

    def test_frozen_empty_pair_is_reported_as_damaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_tools = root / "bundle" / "video_tools"
            bundle_tools.mkdir(parents=True)
            for name in ("ffmpeg", "ffprobe"):
                (bundle_tools / f"{name}{_TOOL_SUFFIX}").touch()

            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "_MEIPASS", str(root / "bundle"), create=True
            ):
                with self.assertRaises(FFmpegNotFoundError):
                    converter_find_tools()

    def test_explicit_override_wins_over_frozen_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            override = root / "override"
            override.mkdir()
            bundle_tools = root / "bundle" / "video_tools"
            bundle_tools.mkdir(parents=True)
            for directory in (override, bundle_tools):
                for name in ("ffmpeg", "ffprobe"):
                    (directory / f"{name}{_TOOL_SUFFIX}").write_bytes(b"binary")

            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "_MEIPASS", str(root / "bundle"), create=True
            ):
                ffmpeg, _ffprobe = converter_find_tools(override)

            self.assertEqual(ffmpeg, str((override / f"ffmpeg{_TOOL_SUFFIX}").resolve()))

    def test_source_checkout_uses_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path_dir = Path(temporary)
            for name in ("ffmpeg", "ffprobe"):
                tool = path_dir / f"{name}{_TOOL_SUFFIX}"
                tool.write_bytes(b"binary")
                tool.chmod(tool.stat().st_mode | stat.S_IXUSR)

            with mock.patch.dict(os.environ, {"PATH": str(path_dir)}):
                ffmpeg, ffprobe = converter_find_tools()

            self.assertEqual(Path(ffmpeg).name, f"ffmpeg{_TOOL_SUFFIX}")
            self.assertEqual(Path(ffprobe).name, f"ffprobe{_TOOL_SUFFIX}")
            self.assertEqual(Path(ffmpeg).parent, Path(ffprobe).parent)


class ToolValidationTests(unittest.TestCase):
    """Verify the minimum version and capability policy."""

    def test_supported_pair_passes_and_reports_versions(self) -> None:
        report = converter_verify_tools("ffmpeg", "ffprobe", runner=fake_tool_runner())

        self.assertEqual(report["tools"]["ffmpeg"]["version"], "8.1.2")  # type: ignore[index]
        self.assertEqual(report["minimum_version"], converter_minimum_versions()["ffmpeg"])
        self.assertTrue(report["features"]["libx264"])  # type: ignore[index]
        self.assertTrue(report["features"]["concat"])  # type: ignore[index]

    def test_newer_major_version_is_accepted(self) -> None:
        report = converter_verify_tools(
            "ffmpeg", "ffprobe", runner=fake_tool_runner(version="9.0.0")
        )

        self.assertEqual(report["tools"]["ffprobe"]["version"], "9.0.0")  # type: ignore[index]

    def test_old_version_is_rejected_with_the_minimum(self) -> None:
        with self.assertRaisesRegex(FFmpegCompatibilityError, "8.1.2"):
            converter_verify_tools(
                "ffmpeg", "ffprobe", runner=fake_tool_runner(version="7.1.0")
            )

    def test_missing_encoder_is_rejected(self) -> None:
        with self.assertRaisesRegex(FFmpegCompatibilityError, "libx264"):
            converter_verify_tools(
                "ffmpeg",
                "ffprobe",
                runner=fake_tool_runner(encoders=" A....D aac  AAC\n"),
            )

    def test_missing_concat_demuxer_is_rejected(self) -> None:
        with self.assertRaisesRegex(FFmpegCompatibilityError, "concat"):
            converter_verify_tools(
                "ffmpeg", "ffprobe", runner=fake_tool_runner(demuxers=" D  matroska\n")
            )

    def test_unparseable_version_is_rejected(self) -> None:
        with self.assertRaises(FFmpegCompatibilityError):
            converter_verify_tools(
                "ffmpeg", "ffprobe", runner=fake_tool_runner(version="N-113000-gabc")
            )


class ConversionGateTests(unittest.TestCase):
    """Verify a hand-picked folder cannot bypass the minimum versions."""

    def setUp(self) -> None:
        converter_module._CONVERTER_TOOL_REPORT_CACHE.clear()

    @unittest.skipIf(os.name == "nt", "POSIX fake tool script")
    def test_selected_old_override_is_rejected_at_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            for name in ("ffmpeg", "ffprobe"):
                script = tools / name
                script.write_text(
                    f"#!/bin/sh\necho '{name} version 8.0.0'\n", encoding="utf-8"
                )
                script.chmod(script.stat().st_mode | stat.S_IXUSR)
            clip = root / "2026" / "07" / "17" / "front" / "001.media"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"clip")
            group = MediaGroup(
                day_root=root / "2026" / "07" / "17",
                year="2026",
                month="07",
                day="17",
                category="front",
                files=(clip,),
            )
            options = ConversionOptions(
                output_root=root / "output",
                output_layout=OutputLayout.FLAT,
                video_format=VideoFormat.MP4_H264,
                naming_mode=NamingMode.MONTH_DAY,
                ffmpeg_directory=tools,
            )

            with self.assertRaises(FFmpegCompatibilityError):
                converter_module.converter_convert((group,), options)


class BuilderTests(unittest.TestCase):
    """Verify the packaging command, guard rails, manifest, and notices."""

    def test_pyinstaller_command_is_onedir_windowed_with_bundled_tools(self) -> None:
        command = build_app.build_app_pyinstaller_command(
            "App",
            Path("/repo/run_app.py"),
            ((Path("/tools/ffmpeg"), "video_tools"), (Path("/tools/ffprobe"), "video_tools")),
            Path("/out/dist"),
            Path("/out/build"),
            platform_name="darwin",
        )

        self.assertNotIn("--onefile", command)
        self.assertIn("--onedir", command)
        self.assertIn("--windowed", command)
        self.assertIn(f"{Path('/tools/ffmpeg')}:video_tools", command)
        self.assertIn(f"{Path('/tools/ffprobe')}:video_tools", command)
        self.assertIn("--osx-bundle-identifier", command)
        self.assertIn(str(Path("/repo/run_app.py")), command)

    def test_windows_command_uses_semicolon_separator(self) -> None:
        command = build_app.build_app_pyinstaller_command(
            "App",
            Path("C:/repo/run_app.py"),
            ((Path("C:/tools/ffmpeg.exe"), "video_tools"),),
            Path("C:/out/dist"),
            Path("C:/out/build"),
            platform_name="win32",
        )

        self.assertIn(f"{Path('C:/tools/ffmpeg.exe')};video_tools", command)
        self.assertNotIn("--osx-bundle-identifier", command)

    def test_missing_runtime_preflight_refuses_to_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            def failing_preflight():
                raise build_app.BuildError("runtime module missing")

            code = build_app.build_app_main(
                [
                    "--output-dir",
                    str(Path(temporary) / "dist"),
                    "--work-dir",
                    str(Path(temporary) / "build"),
                ],
                preflight=failing_preflight,
            )

            self.assertEqual(code, 1)
            self.assertFalse((Path(temporary) / "dist" / "dist").exists())

    def test_unsupported_tools_refuse_to_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary) / "tools"
            tools.mkdir()
            for name in ("ffmpeg", "ffprobe"):
                (tools / f"{name}{_TOOL_SUFFIX}").write_bytes(b"binary")

            code = build_app.build_app_main(
                [
                    "--ffmpeg-bin",
                    str(tools),
                    "--output-dir",
                    str(Path(temporary) / "dist"),
                    "--work-dir",
                    str(Path(temporary) / "build"),
                ],
                preflight=lambda: {"ok": True},
                runner=fake_tool_runner(version="7.0.0"),
            )

            self.assertEqual(code, 1)

    def test_manifest_records_versions_hashes_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tool_file = Path(temporary) / "ffmpeg"
            tool_file.write_bytes(b"binary")
            digest = build_app.build_app_sha256(tool_file)
            report = converter_verify_tools("ffmpeg", "ffprobe", runner=fake_tool_runner())

            manifest = build_app.build_app_manifest(
                "App",
                "macos-app",
                Path(temporary) / "App.app",
                report,
                {"ffmpeg": digest, "ffprobe": digest},
                "LICENSE.txt",
                "2026-10-05T00:00:00Z",
            )
            payload = json.dumps(manifest, sort_keys=True)

            self.assertIn(digest, payload)
            self.assertEqual(manifest["tools"]["ffmpeg"]["version"], "8.1.2")  # type: ignore[index]
            self.assertEqual(
                manifest["policy"]["minimum_versions"]["ffmpeg"], "8.1.2"  # type: ignore[index]
            )
            self.assertIn("--self-test", payload)

    def test_notices_derive_licence_from_build_configuration(self) -> None:
        report = converter_verify_tools(
            "ffmpeg", "ffprobe", runner=fake_tool_runner(configuration="--enable-gpl")
        )

        notices = build_app.build_app_notices(report, "Homebrew", None)

        self.assertIn("gpl=True", notices)
        self.assertIn("not assumed", notices)
        self.assertIn("https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz", notices)

    def test_embedded_tool_verification_flags_missing_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            problems = build_app.build_app_verify_embedded_tools(
                Path(temporary), {"ffmpeg": "abc", "ffprobe": "abc"}
            )

            self.assertEqual(len(problems), 2)


if __name__ == "__main__":
    unittest.main()
