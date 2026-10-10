"""Keep only the parts of a video where a dog is on screen.

Local-only two-step workflow (nothing is uploaded, originals never change):

  1. analyze: sample frames, run a small YOLO detector, write a reviewable
     segments.csv with preview pictures, every frame's score in hits.csv,
     and check pictures for long deleted stretches.
  2. export:  build the dogs-only video from the reviewed list (fast copy).

Run with the repo sandbox (needs ultralytics there)::

    .venv/bin/python scripts/dog_filter.py analyze INPUT.mp4 --out workdir
    .venv/bin/python scripts/dog_filter.py export INPUT.mp4 workdir/segments.csv --out dogs.mp4
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run_ffmpeg(args: list[str]) -> None:
    """Run one FFmpeg command or stop with its own error text."""

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown FFmpeg error"
        raise SystemExit(f"FFmpeg failed: {detail}")


def probe_duration(path: Path) -> float:
    """Return the video length in seconds."""

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"FFprobe cannot read: {path}")
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        raise SystemExit(f"FFprobe found no duration in: {path}")


def stamp(seconds: float) -> str:
    """Format seconds as H:MM:SS for readable logs."""

    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def widen_segments(segments: list[dict], before: float, after: float,
                   duration: float) -> list[dict]:
    """Add calm handles each side of every segment, clamped to the video."""

    return [
        {"start": max(0.0, seg["start"] - before),
         "end": min(duration, seg["end"] + after),
         "best": seg["best"]}
        for seg in segments
    ]


def cmd_analyze(args: argparse.Namespace) -> None:
    """Sample frames, detect dogs, and write a reviewable segment list."""

    from ultralytics import YOLO

    source = Path(args.input).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Input video not found: {source}")
    out = Path(args.out).expanduser().resolve()
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(source)
    print(f"Video is {stamp(duration)} long. Pulling one frame every {args.every:g}s...")
    run_ffmpeg(["-y", "-i", str(source), "-vf", f"fps=1/{args.every:g}",
                "-q:v", "3", str(frames_dir / "frame_%06d.jpg")])
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    if not frames:
        raise SystemExit("No frames came out; is the input a playable video?")
    print(f"Checking {len(frames)} frames for dogs (first run downloads the model)...")

    model = YOLO(args.model)
    dog_id = next(
        (number for number, name in model.names.items() if name == "dog"), None
    )
    if dog_id is None:
        raise SystemExit("This model has no 'dog' class; try a COCO-trained model.")

    hits: list[dict] = []
    for index, frame in enumerate(frames, start=1):
        moment = (index - 1) * args.every
        if moment > duration:
            break
        best = 0.0
        for result in model.predict(str(frame), imgsz=args.imgsz, verbose=False):
            for box in result.boxes:
                if int(box.cls[0]) == dog_id:
                    best = max(best, float(box.conf[0]))
        hits.append({"t": round(moment, 2), "conf": round(best, 3)})
        if index % 100 == 0 or index == len(frames):
            found = sum(1 for hit in hits if hit["conf"] >= args.conf)
            print(f"  ...{index}/{len(frames)} frames, dog so far in {found}")

    hits_path = out / "hits.csv"
    with hits_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame", "t", "conf"])
        for frame, hit in zip(frames, hits):
            writer.writerow([frame.name, f'{hit["t"]:.2f}', f'{hit["conf"]:.3f}'])

    # Merge nearby hits into segments, then widen each with calm handles.
    segments: list[dict] = []
    open_start: float | None = None
    open_best = 0.0
    previous = 0.0
    for hit in hits + [{"t": duration + args.merge_gap + 1, "conf": 0.0}]:
        if hit["conf"] >= args.conf:
            if open_start is None:
                open_start = hit["t"]
            open_best = max(open_best, hit["conf"])
            previous = hit["t"]
        elif open_start is not None and hit["t"] - previous > args.merge_gap:
            segments.append({"start": open_start, "end": previous, "best": open_best})
            open_start = None
            open_best = 0.0
    before = args.handles_before if args.handles_before is not None else (
        args.handles if args.handles is not None else 8.0)
    after = args.handles_after if args.handles_after is not None else (
        args.handles if args.handles is not None else 3.0)
    segments = [
        seg for seg in widen_segments(segments, before, after, duration)
        if seg["end"] - seg["start"] >= args.min_len
    ]
    # Re-join segments whose new handles now touch.
    joined: list[dict] = []
    for seg in segments:
        if joined and seg["start"] <= joined[-1]["end"]:
            joined[-1]["end"] = max(joined[-1]["end"], seg["end"])
            joined[-1]["best"] = max(joined[-1]["best"], seg["best"])
        else:
            joined.append(seg)

    gaps_dir = out / "gaps"
    gaps_dir.mkdir(exist_ok=True)
    deleted_path = out / "deleted.csv"
    with deleted_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["n", "start", "end", "duration", "samples"])
        gap_count = 0
        edges = [0.0] + [mark for seg in joined for mark in (seg["start"], seg["end"])] + [duration]
        for gap_start, gap_end in zip(edges[0::2], edges[1::2]):
            if gap_end - gap_start >= args.gap_review:
                gap_count += 1
                thumbs: list[str] = []
                for part, frac in enumerate((0.25, 0.5, 0.75), start=1):
                    target = gap_start + (gap_end - gap_start) * frac
                    nearest = min(frames, key=lambda frame: abs(
                        (int(frame.stem.split("_")[1]) - 1) * args.every - target))
                    name = f"gap_{gap_count:03d}_{part}.jpg"
                    shutil.copy(nearest, gaps_dir / name)
                    thumbs.append(name)
                writer.writerow([gap_count, f"{gap_start:.2f}", f"{gap_end:.2f}",
                                 f"{gap_end - gap_start:.1f}", ";".join(thumbs)])

    thumbs_dir = out / "thumbs"
    thumbs_dir.mkdir(exist_ok=True)
    csv_path = out / "segments.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["n", "start", "end", "duration", "best_conf", "thumb"])
        for number, seg in enumerate(joined, start=1):
            middle = min(
                frames,
                key=lambda frame: abs(
                    (int(frame.stem.split("_")[1]) - 1) * args.every
                    - (seg["start"] + seg["end"]) / 2
                ),
            )
            thumb = thumbs_dir / f"seg_{number:03d}.jpg"
            shutil.copy(middle, thumb)
            writer.writerow([number, f'{seg["start"]:.2f}', f'{seg["end"]:.2f}',
                             f'{seg["end"] - seg["start"]:.1f}',
                             f'{seg["best"]:.2f}', thumb.name])
    (out / "analysis.json").write_text(json.dumps({
        "input": str(source), "duration": duration, "model": args.model,
        "every": args.every, "conf": args.conf, "frames": len(hits),
        "frames_with_dog": sum(1 for hit in hits if hit["conf"] >= args.conf),
        "segments": len(joined),
        "kept_seconds": round(sum(seg["end"] - seg["start"] for seg in joined), 1),
    }, indent=2))

    print(f"\nKept {len(joined)} dog parts, "
          f"{sum(seg['end'] - seg['start'] for seg in joined):.0f}s of {duration:.0f}s.")
    for row_number, seg in enumerate(joined, start=1):
        print(f"  {row_number}. {stamp(seg['start'])} - {stamp(seg['end'])} "
              f"(best {seg['best']:.2f})")
    print(f"\nReview {csv_path.name} and the pictures in {thumbs_dir.name}/, "
          f"delete or fix any rows, then run:\n"
          f"  .venv/bin/python scripts/dog_filter.py export {source} {csv_path} --out dogs.mp4")
    print(f"Every frame's score is in {hits_path.name} (frame name, time, "
          f"confidence). Long deleted stretches have check pictures in "
          f"{gaps_dir.name}/, listed in {deleted_path.name}.")


def cmd_export(args: argparse.Namespace) -> None:
    """Build the dogs-only video from a (possibly hand-edited) segment list."""

    source = Path(args.input).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Input video not found: {source}")
    rows = list(csv.DictReader(Path(args.segments).expanduser().read_text().splitlines()))
    if not rows:
        raise SystemExit("The segment list is empty; nothing to export.")
    target = Path(args.out).expanduser().resolve()
    if target.exists():
        raise SystemExit(f"Refusing to overwrite: {target} (move it first).")

    work = target.parent / f".{target.stem}.parts"
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for number, row in enumerate(rows, start=1):
            part = work / f"part_{number:03d}.mp4"
            run_ffmpeg(["-ss", row["start"], "-to", row["end"], "-i", str(source),
                        "-c", "copy", str(part)])
            parts.append(part)
            print(f"  cut {number}/{len(rows)}: {stamp(float(row['start']))} - "
                  f"{stamp(float(row['end']))}")
        concat = work / "list.txt"
        concat.write_text(
            "ffconcat version 1.0\n"
            + "".join(f"file '{part.resolve().as_posix()}'\n" for part in parts)
        )
        partial = target.with_name(f".{target.stem}.partial{target.suffix}")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat),
                    "-c", "copy", "-movflags", "+faststart", str(partial)])
        partial.replace(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"Done: {target} ({len(parts)} parts joined). Originals untouched.")


def main(argv: list[str]) -> None:
    """Parse the analyze/export sub-commands."""

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="find dog segments, write review list")
    analyze.add_argument("input", help="source video file")
    analyze.add_argument("--out", required=True, help="working folder for results")
    analyze.add_argument("--every", type=float, default=2.0,
                         help="seconds between checked frames (default 2)")
    analyze.add_argument("--conf", type=float, default=0.25,
                         help="keep frames at/above this dog confidence (default 0.25)")
    analyze.add_argument("--merge-gap", type=float, default=10.0,
                         help="join hits this close in seconds (default 10)")
    analyze.add_argument("--min-len", type=float, default=2.0,
                         help="drop kept parts shorter than this (default 2s)")
    analyze.add_argument("--handles", type=float, default=None,
                         help="calm seconds added each side; overridden per side by "
                              "--handles-before/--handles-after")
    analyze.add_argument("--handles-before", type=float, default=None,
                         help="calm seconds added before each kept part (default 8)")
    analyze.add_argument("--handles-after", type=float, default=None,
                         help="calm seconds added after each kept part (default 3)")
    analyze.add_argument("--gap-review", type=float, default=120.0,
                         help="deleted stretches at/over this length get check "
                              "pictures (default 120s)")
    analyze.add_argument("--model", default=str(
        Path.home() / ".cache" / "dog_filter" / "yolov8s.pt"),
        help="detector weights (downloaded once on first run)")
    analyze.add_argument("--imgsz", type=int, default=960,
                         help="detection image size; bigger sees small dogs, slower (default 960)")

    export = sub.add_parser("export", help="build dogs-only video from review list")
    export.add_argument("input", help="source video file")
    export.add_argument("segments", help="reviewed segments.csv")
    export.add_argument("--out", required=True, help="output MP4 path")

    parsed = parser.parse_args(argv)
    if parsed.command == "analyze":
        cmd_analyze(parsed)
    else:
        cmd_export(parsed)


if __name__ == "__main__":
    main(sys.argv[1:])
