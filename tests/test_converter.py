"""Tests for output naming and dependency discovery policies."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from mediatovideo_converter.converter import (
    ConversionProcessError,
    FFmpegNotFoundError,
    _converter_try_demux_littlelf,
    converter_collect_mp4s,
    converter_convert,
    converter_convert_mkv_to_mp4,
    converter_find_tools,
    converter_join_mp4s,
    converter_order_creation_key,
    converter_order_folder_key,
    converter_order_time_key,
    converter_plan_targets,
)
from mediatovideo_converter.models import (
    ConversionOptions,
    MediaGroup,
    NamingMode,
    OutputLayout,
    VideoFormat,
)


class ConverterPlanningTests(unittest.TestCase):
    """Verify portable, deterministic output paths."""

    def test_mirrored_category_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = self._group(root, "Front: Camera")
            options = ConversionOptions(
                output_root=root / "output",
                output_layout=OutputLayout.MIRROR_DATES,
                video_format=VideoFormat.MP4_H264,
                naming_mode=NamingMode.MONTH_DAY_CATEGORY,
            )

            targets = converter_plan_targets((group,), options)

            self.assertEqual(
                targets[0],
                root / "output" / "2026" / "07" / "17" / "07-17-Front- Camera.mp4",
            )

    def test_flat_duplicate_names_receive_numeric_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            groups = (self._group(root, "Front"), self._group(root, "Rear"))
            options = ConversionOptions(
                output_root=root / "output",
                output_layout=OutputLayout.FLAT,
                video_format=VideoFormat.MKV_COPY,
                naming_mode=NamingMode.MONTH_DAY,
            )

            targets = converter_plan_targets(groups, options)

            self.assertEqual(targets[0].name, "07-17.mkv")
            self.assertEqual(targets[1].name, "07-17-2.mkv")

    def test_custom_ffmpeg_folder_requires_both_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            suffix = ".exe" if os.name == "nt" else ""
            (directory / f"ffmpeg{suffix}").touch()

            with self.assertRaises(FFmpegNotFoundError):
                converter_find_tools(directory)

            (directory / f"ffprobe{suffix}").touch()
            ffmpeg, ffprobe = converter_find_tools(directory)
            self.assertTrue(ffmpeg.endswith(f"ffmpeg{suffix}"))
            self.assertTrue(ffprobe.endswith(f"ffprobe{suffix}"))

    def test_mkv_converter_refuses_to_overwrite_existing_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mkv"
            target = root / "source.mp4"
            source.write_bytes(b"mkv")
            target.write_bytes(b"existing")

            with self.assertRaisesRegex(
                ConversionProcessError, "will not be overwritten"
            ):
                converter_convert_mkv_to_mp4(source, target)

            self.assertEqual(target.read_bytes(), b"existing")

    def test_mkv_converter_requires_mkv_input_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.avi"
            source.write_bytes(b"video")

            with self.assertRaisesRegex(ConversionProcessError, "not an MKV"):
                converter_convert_mkv_to_mp4(source, root / "output.mp4")

    @unittest.skipIf(os.name == "nt", "POSIX fake executable smoke test")
    def test_conversion_pipeline_publishes_completed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            self._write_fake_tool(
                tools / "ffprobe",
                "import sys\nprint('1.0')\n",
            )
            self._write_fake_tool(
                tools / "ffmpeg",
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[-1]).write_bytes(b'converted')\n"
                "print('out_time_us=1000000', flush=True)\n"
                "print('progress=end', flush=True)\n",
            )
            group = self._group(root, "Front")
            group.files[0].parent.mkdir(parents=True)
            group.files[0].write_bytes(b"clip")
            options = ConversionOptions(
                output_root=root / "output",
                output_layout=OutputLayout.FLAT,
                video_format=VideoFormat.MKV_COPY,
                naming_mode=NamingMode.MONTH_DAY_CATEGORY,
                ffmpeg_directory=tools,
            )
            events: list[str] = []

            summary = converter_convert(
                (group,), options, event=lambda name, _details: events.append(name)
            )

            self.assertFalse(summary.cancelled)
            self.assertFalse(summary.failed_groups)
            self.assertEqual(len(summary.completed), 1)
            self.assertEqual(summary.completed[0].read_bytes(), b"converted")
            self.assertIn("encoding_progress", events)
            self.assertIn("group_completed", events)

    @unittest.skipIf(os.name == "nt", "POSIX fake executable smoke test")
    def test_mkv_conversion_pipeline_publishes_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            self._write_fake_tool(tools / "ffprobe", "print('2.0')\n")
            self._write_fake_tool(
                tools / "ffmpeg",
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[-1]).write_bytes(b'mp4')\n"
                "print('out_time_us=2000000', flush=True)\n"
                "print('progress=end', flush=True)\n",
            )
            source = root / "recording.mkv"
            target = root / "recording.mp4"
            source.write_bytes(b"mkv")
            events: list[str] = []

            result = converter_convert_mkv_to_mp4(
                source,
                target,
                ffmpeg_directory=tools,
                event=lambda name, _details: events.append(name),
            )

            self.assertFalse(result.cancelled)
            self.assertEqual(result.output, target.resolve())
            self.assertEqual(target.read_bytes(), b"mp4")
            self.assertIn("single_started", events)
            self.assertIn("encoding_progress", events)
            self.assertIn("single_completed", events)

    @staticmethod
    def _group(root: Path, category: str) -> MediaGroup:
        """Build a representative one-clip group."""

        clip = root / "2026" / "07" / "17" / category / "001.media"
        return MediaGroup(
            day_root=root / "2026" / "07" / "17",
            year="2026",
            month="07",
            day="17",
            category=category,
            files=(clip,),
        )

    @staticmethod
    def _write_fake_tool(path: Path, body: str) -> None:
        """Write an executable Python command used as a deterministic test tool."""

        preflight = (
            "import sys\n"
            f"if '-version' in sys.argv: print('{path.name} version 8.1.2'); sys.exit(0)\n"
            "if '-encoders' in sys.argv: print(' V..... libx264\\n A..... aac'); sys.exit(0)\n"
            "if '-demuxers' in sys.argv: print(' D concat'); sys.exit(0)\n"
        )
        path.write_text(f"#!{sys.executable}\n{preflight}{body}", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


class LittlelfDemuxTests(unittest.TestCase):
    """Verify hidden type-3 PCM sound is split from picture."""

    @staticmethod
    def _frame(frame_type: int, payload: bytes) -> bytes:
        import struct

        header = struct.pack("<IIQII", frame_type, len(payload), 1646138013913, 0, 20)
        return header + payload

    def test_demux_splits_video_and_audio(self) -> None:
        video = b"\x00\x00\x00\x01\x65\x01\x02"
        audio = b"\x08\x00\xf8\xff" * 160
        data = (
            self._frame(1, video)
            + self._frame(3, audio)
            + self._frame(0, video)
        )

        split = _converter_try_demux_littlelf(data)

        self.assertIsNotNone(split)
        assert split is not None
        self.assertEqual(split[0], video + video)
        self.assertEqual(split[1], audio)

    def test_demux_rejects_plain_h264(self) -> None:
        self.assertIsNone(_converter_try_demux_littlelf(b"clip"))
        self.assertIsNone(_converter_try_demux_littlelf(b"\x00\x00\x00\x01\x65"))

    @unittest.skipIf(os.name == "nt", "POSIX fake executable smoke test")
    def test_conversion_with_audio_maps_both_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            ConverterPlanningTests._write_fake_tool(
                tools / "ffprobe",
                "import sys\nprint('1.0')\n",
            )
            ConverterPlanningTests._write_fake_tool(
                tools / "ffmpeg",
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[-1]).write_bytes(b'converted')\n"
                "with open(pathlib.Path(__file__).parent / 'calls.log', 'a') as log:\n"
                "    log.write(' '.join(sys.argv) + chr(10))\n"
                "print('out_time_us=1000000', flush=True)\n"
                "print('progress=end', flush=True)\n",
            )
            clip = root / "clip.media"
            video = b"\x00\x00\x00\x01\x65\x01\x02"
            audio = b"\x08\x00\xf8\xff" * 160
            clip.write_bytes(self._frame(1, video) + self._frame(3, audio))
            group = MediaGroup(
                day_root=root,
                year="2026",
                month="07",
                day="17",
                category=None,
                files=(clip,),
            )
            options = ConversionOptions(
                output_root=root / "output",
                output_layout=OutputLayout.FLAT,
                video_format=VideoFormat.MP4_H264,
                naming_mode=NamingMode.MONTH_DAY,
                ffmpeg_directory=tools,
            )

            tmpdir = root / "tmp"
            tmpdir.mkdir()
            old_tmpdir = os.environ.get("TMPDIR")
            os.environ["TMPDIR"] = str(tmpdir)
            try:
                summary = converter_convert((group,), options)
            finally:
                if old_tmpdir is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = old_tmpdir

            self.assertFalse(summary.failed_groups)
            self.assertEqual(len(summary.completed), 1)
            combined = (tools / "calls.log").read_text()
            self.assertIn("s16le", combined)
            self.assertIn("1:a:0?", combined)
            self.assertIn(" -t ", combined)
            self.assertNotIn("-shortest", combined)


class JoinMp4Tests(unittest.TestCase):
    """Verify folder-wide MP4 joining keeps time order and matching tracks."""

    def test_time_order_sorts_by_date_then_event_epoch(self) -> None:
        late = Path("Pieces") / "09-24-1632510909_0015.mp4"
        early = Path("Pieces") / "09-24-1632510732_0013.mp4"
        next_day = Path("Pieces") / "12-29-1640798622_0015.mp4"

        ordered = sorted((late, next_day, early), key=converter_order_time_key)

        self.assertEqual(ordered, [early, late, next_day])

    def test_folder_order_matches_scanner_discovery(self) -> None:
        paths = [Path("b.mp4"), Path("A.mp4")]

        self.assertEqual(
            sorted(paths, key=converter_order_folder_key),
            [Path("A.mp4"), Path("b.mp4")],
        )

    def test_collect_finds_nested_mp4s_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "2021" / "09" / "24").mkdir(parents=True)
            (root / "flat").mkdir()
            (root / "2021" / "09" / "24" / "b.mp4").write_bytes(b"x")
            (root / "flat" / "a.mp4").write_bytes(b"x")
            (root / "notes.txt").write_text("ignored")

            found = converter_collect_mp4s(root)

            self.assertEqual(
                [path.name for path in found], ["b.mp4", "a.mp4"]
            )

    def test_collect_accepts_mkv_pieces_when_asked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "09-24.mkv").write_bytes(b"x")
            (root / "09-24.mp4").write_bytes(b"x")

            default = converter_collect_mp4s(root)
            both = converter_collect_mp4s(root, (".mp4", ".mkv"))

            self.assertEqual([path.suffix for path in default], [".mp4"])
            self.assertEqual(len(both), 2)

    def test_creation_order_follows_modification_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "09-24-299.mkv"
            second = root / "09-24.mkv"
            first.write_bytes(b"x")
            second.write_bytes(b"x")
            os.utime(first, (1_000_000_000, 1_000_000_000))
            os.utime(second, (1_000_000_001, 1_000_000_001))

            self.assertEqual(
                sorted((second, first), key=converter_order_creation_key),
                [first, second],
            )

    def test_join_refuses_single_file_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            only = root / "only.mp4"
            only.write_bytes(b"x")

            with self.assertRaisesRegex(ConversionProcessError, "at least two"):
                converter_join_mp4s((only,), root / "out.mp4")

            existing = root / "out.mp4"
            existing.write_bytes(b"kept")
            other = root / "other.mp4"
            other.write_bytes(b"x")
            with self.assertRaisesRegex(ConversionProcessError, "will not be overwritten"):
                converter_join_mp4s((only, other), existing)
            self.assertEqual(existing.read_bytes(), b"kept")

    def test_join_rejects_non_video_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "a.mp4"
            bad = root / "b.avi"
            good.write_bytes(b"x")
            bad.write_bytes(b"x")

            with self.assertRaisesRegex(ConversionProcessError, "MP4 or MKV"):
                converter_join_mp4s((good, bad), root / "out.mp4")

    @unittest.skipIf(os.name == "nt", "POSIX fake executable smoke test")
    def test_join_pipeline_uses_copy_in_listed_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            ConverterPlanningTests._write_fake_tool(
                tools / "ffprobe",
                "print('codec_name=h264')\n"
                "print('codec_type=video')\n"
                "print('codec_name=aac')\n"
                "print('codec_type=audio')\n"
                "print('duration=2.0')\n",
            )
            ConverterPlanningTests._write_fake_tool(
                tools / "ffmpeg",
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[-1]).write_bytes(b'joined')\n"
                "with open(pathlib.Path(__file__).parent / 'calls.log', 'a') as log:\n"
                "    log.write(' '.join(sys.argv) + chr(10))\n"
                "print('out_time_us=2000000', flush=True)\n"
                "print('progress=end', flush=True)\n",
            )
            first = root / "09-24-1632510732_0013.mkv"
            second = root / "09-24-1632510909_0015.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            target = root / "day.mp4"
            seen: list[tuple[str, dict]] = []

            result = converter_join_mp4s(
                (second, first),
                target,
                ffmpeg_directory=tools,
                event=lambda name, details: seen.append((name, details)),
            )

            self.assertFalse(result.cancelled)
            self.assertEqual(target.read_bytes(), b"joined")
            calls = (tools / "calls.log").read_text()
            self.assertIn("-c copy", calls)
            started = [details for name, details in seen if name == "single_started"]
            self.assertEqual(len(started), 1)
            self.assertEqual(
                started[0]["sources"], [str(second.resolve()), str(first.resolve())]
            )
            self.assertIn("single_completed", [name for name, _ in seen])


if __name__ == "__main__":
    unittest.main()
