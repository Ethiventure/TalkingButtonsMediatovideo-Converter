"""Build the self-contained Mediatovideo Converter bundle.

The old helper built a one-file executable without FFmpeg or FFprobe, so the
frozen app used whatever was installed on the machine. This tool builds a
native onedir bundle that carries its own Python, Tk, FFmpeg and FFprobe,
records the versions and hashes it packaged, and refuses to build when the
runtime or the tools cannot be verified. Version and capability policy lives in
``mediatovideo_converter.converter``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from mediatovideo_converter import __version__ as APPLICATION_VERSION
from mediatovideo_converter.converter import (
    FFmpegCompatibilityError,
    FFmpegNotFoundError,
    converter_bundled_tools_dirname,
    converter_find_tools,
    converter_minimum_versions,
    converter_verify_tools,
)

DEFAULT_APPLICATION_NAME = "Mediatovideo Converter"
MACOS_BUNDLE_IDENTIFIER = "com.jaredreabow.mediatovideo-converter"
NOTICES_NAME = "THIRD-PARTY-NOTICES.txt"
ToolRunner = Callable[[Sequence[str]], tuple[int, str]]


class BuildError(RuntimeError):
    """Raised when the bundle cannot be produced or verified safely."""


def build_app_pyinstaller_command(
    application_name: str,
    entry_point: Path,
    binaries: Sequence[tuple[Path, str]],
    dist_dir: Path,
    work_dir: Path,
    platform_name: str | None = None,
) -> list[str]:
    """Return the PyInstaller command for the native onedir bundle.

    ``--onedir`` with ``--windowed`` produces a macOS ``.app`` and a Windows
    folder with an ``.exe``. PyInstaller's tkinter hook collects Tk, and its
    binary analysis follows the linked libraries of each ``--add-binary``
    entry, so FFmpeg's own dependencies are bundled too.
    """

    host_platform = platform_name or sys.platform
    separator = ";" if host_platform == "win32" else ":"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--noupx",
        "--name",
        application_name,
        "--paths",
        str(REPOSITORY_ROOT),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir / "spec"),
    ]
    if host_platform == "darwin":
        command.extend(["--osx-bundle-identifier", MACOS_BUNDLE_IDENTIFIER])
    for source, destination in binaries:
        command.extend(["--add-binary", f"{source}{separator}{destination}"])
    command.append(str(entry_point))
    return command


def build_app_embedded_tools_root(
    dist_dir: Path, application_name: str, platform_name: str | None = None
) -> Path:
    """Return the packaged ``video_tools`` folder inside the built bundle."""

    if (platform_name or sys.platform) == "darwin":
        frameworks = dist_dir / f"{application_name}.app" / "Contents" / "Frameworks"
        return frameworks / converter_bundled_tools_dirname()
    return (
        dist_dir / application_name / "_internal" / converter_bundled_tools_dirname()
    )


def build_app_verify_embedded_tools(tools_root: Path, hashes: dict[str, str]) -> list[str]:
    """Return the problems that make the packaged video tools unusable."""

    problems: list[str] = []
    suffix = ".exe" if sys.platform == "win32" else ""
    for name in ("ffmpeg", "ffprobe"):
        candidate = tools_root / f"{name}{suffix}"
        if not candidate.is_file() or candidate.stat().st_size == 0:
            problems.append(f"missing packaged tool: {candidate}")

    return problems


def build_app_runtime_preflight() -> dict[str, object]:
    """Run the runtime module's check, or refuse to build.

    The runtime module owns the Python and Tk policy. A bundle must not be
    produced when that check is unavailable or fails.
    """

    try:
        from mediatovideo_converter.runtime import runtime_check
    except ImportError as error:
        raise BuildError(
            "The runtime module (mediatovideo_converter.runtime) is not available, "
            f"so the frozen runtime cannot be checked. Import error: {error}"
        ) from error
    report = runtime_check()
    return report if isinstance(report, dict) else {"detail": str(report)}


def build_app_notices(
    tools_report: dict[str, object], provider: str, license_path: Path | None
) -> str:
    """Return the third-party notice shipped inside the bundle.

    The FFmpeg licence is derived from the tools' own build configuration
    rather than assumed, because FFmpeg builds ship under different licences
    depending on their configure flags.
    """

    tools = tools_report.get("tools") or {}
    tools = tools if isinstance(tools, dict) else {}
    ffmpeg_entry = tools.get("ffmpeg") if isinstance(tools.get("ffmpeg"), dict) else {}
    lines = ["THIRD-PARTY NOTICES", "===================", "",
             "The packaged application includes FFmpeg and FFprobe.", ""]
    for name in ("ffmpeg", "ffprobe"):
        entry = tools.get(name) if isinstance(tools.get(name), dict) else {}
        lines.append(f"{name}: {entry.get('banner') or 'version banner unavailable'}")
    configuration = str(ffmpeg_entry.get("configuration") or "")
    lines.extend(
        [
            f"build configuration: {configuration or 'not reported by this build'}",
            f"licence flags detected in that configuration: gpl={_flag(configuration, 'gpl')} "
            f"version3={_flag(configuration, 'version3')} nonfree={_flag(configuration, 'nonfree')}",
            "licence: not assumed here; FFmpeg is distributed under the licence its "
            "build configuration selects.",
            "project home: https://ffmpeg.org/",
            f"provider: {provider}",
        ]
    )
    version = str(ffmpeg_entry.get("version") or "")
    if version:
        lines.append(
            f"corresponding source: https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz"
        )
    if license_path is not None:
        lines.append(f"provider licence file packaged as FFMPEG-LICENSE.txt ({license_path})")
    else:
        lines.append("no provider licence file supplied; pass --ffmpeg-license when available")
    lines.append("")
    return "\n".join(lines)


def _flag(configuration: str, name: str) -> bool:
    """Return True when a configure flag such as ``gpl`` is present."""

    return f"--enable-{name}" in configuration


def build_app_manifest(
    application_name: str,
    output_kind: str,
    output_path: Path,
    tools_report: dict[str, object],
    hashes: dict[str, str],
    license_name: str | None,
    generated_utc: str,
) -> dict[str, object]:
    """Return the provenance manifest written beside and inside the bundle."""

    tools = tools_report.get("tools") or {}
    tools = tools if isinstance(tools, dict) else {}
    return {
        "application": {
            "name": application_name,
            "version": APPLICATION_VERSION,
            "entry_point": "run_app.py",
        },
        "build": {
            "generated_utc": generated_utc,
            "platform": sys.platform,
            "machine": platform.machine(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "pyinstaller": _package_version("pyinstaller"),
            "tk": _tk_version(),
        },
        "output": {"kind": output_kind, "name": output_path.name, "path": str(output_path)},
        "policy": {
            "bundled_tools_dirname": converter_bundled_tools_dirname(),
            "minimum_versions": converter_minimum_versions(),
        },
        "tools": tools,
        "hashes": hashes,
        "licenses": {
            "third_party_notices": NOTICES_NAME,
            "project_license": license_name,
        },
        "verification": {
            "frozen_self_test": ["<bundle executable>", "--self-test", "--self-test-report", "selftest.json"],
            "run_with_cleared_path": True,
        },
    }


def build_app_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_app_dialog_helper(output_path: Path) -> Path:
    """Compile and sign the independent macOS startup reporter.

    The runtime module owns the constant argv-only dialog source and helper
    name. A foreground applet has a stable LaunchServices identity, so failures
    and the repair button remain usable when Tk cannot start.
    """
    from mediatovideo_converter.runtime import (
        runtime_dialog_helper_name,
        runtime_dialog_helper_source,
    )

    helper = output_path / "Contents" / "Frameworks" / runtime_dialog_helper_name()
    compiled = subprocess.run([
        "/usr/bin/osacompile", "-o", str(helper), "-e", runtime_dialog_helper_source(),
    ], check=False, capture_output=True, text=True, timeout=60)
    if compiled.returncode != 0:
        raise BuildError(f"Startup reporter compilation failed: {compiled.stderr.strip()}")
    plist = helper / "Contents" / "Info.plist"
    with plist.open("rb") as stream:
        info = plistlib.load(stream)
    info.update(CFBundleIdentifier=MACOS_BUNDLE_IDENTIFIER + ".startup-reporter",
                CFBundleName="Mediatovideo Startup Reporter",
                CFBundleDisplayName="Mediatovideo Startup Reporter")
    info.pop("LSUIElement", None)
    with plist.open("wb") as stream:
        plistlib.dump(info, stream)
    signed = subprocess.run([
        "/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(helper),
    ], check=False, capture_output=True, text=True, timeout=60)
    if signed.returncode != 0:
        raise BuildError(f"Startup reporter signing failed: {signed.stderr.strip()}")
    return helper


def _package_version(name: str) -> str | None:
    """Return an installed distribution version, or None."""

    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(name)
        except PackageNotFoundError:
            return None
    except Exception:  # noqa: BLE001 - metadata must never break a build
        return None


def _tk_version() -> str | None:
    """Return the Tk version PyInstaller will collect, when available."""

    try:
        import tkinter
    except Exception:  # noqa: BLE001 - a build host may lack Tk
        return None
    return str(tkinter.TkVersion)


def _build_app_provider(ffmpeg_bin: Path | None, ffmpeg: str) -> str:
    """Describe where the packaged tools came from without guessing."""

    if ffmpeg_bin is not None:
        return f"--ffmpeg-bin {ffmpeg_bin}"
    if str(ffmpeg).startswith("/opt/homebrew/"):
        return "Homebrew (Apple Silicon prefix)"
    if str(ffmpeg).startswith("/usr/local/"):
        return "Homebrew (Intel prefix)"
    return f"PATH ({ffmpeg})"


def build_app_main(
    argv: Sequence[str] | None = None,
    preflight: Callable[[], dict[str, object]] | None = None,
    runner: ToolRunner | None = None,
) -> int:
    """Build, describe, and verify the native bundle; return an exit code."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_APPLICATION_NAME)
    parser.add_argument("--output-dir", type=Path, default=REPOSITORY_ROOT / "dist")
    parser.add_argument("--work-dir", type=Path, default=REPOSITORY_ROOT / "build")
    parser.add_argument(
        "--ffmpeg-bin",
        type=Path,
        default=None,
        help="Folder containing both ffmpeg and ffprobe to package.",
    )
    parser.add_argument(
        "--ffmpeg-license",
        type=Path,
        default=None,
        help="Provider licence file to copy into the bundle.",
    )
    arguments = parser.parse_args(argv)
    generated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        runtime_report = (preflight or build_app_runtime_preflight)()
    except Exception as error:  # noqa: BLE001 - every preflight failure is fatal
        print(
            "BUILD REFUSED\n=============\n"
            "Stage: Checking the frozen runtime\n"
            f"Problem: {error}\n"
            "What to do: fix the runtime problem first; a bundle is not produced "
            "when the runtime check cannot run.",
            file=sys.stderr,
        )
        return 1

    try:
        ffmpeg, ffprobe = converter_find_tools(arguments.ffmpeg_bin)
        tools_report = converter_verify_tools(ffmpeg, ffprobe, runner=runner)
    except (FFmpegNotFoundError, FFmpegCompatibilityError) as error:
        print(
            f"{error}\n\nPass --ffmpeg-bin with a folder containing both tools.",
            file=sys.stderr,
        )
        return 1

    hashes = {
        "ffmpeg": build_app_sha256(Path(ffmpeg)),
        "ffprobe": build_app_sha256(Path(ffprobe)),
    }
    dist_dir = arguments.output_dir.resolve()
    command = build_app_pyinstaller_command(
        arguments.name,
        REPOSITORY_ROOT / "run_app.py",
        ((Path(ffmpeg), "video_tools"), (Path(ffprobe), "video_tools")),
        dist_dir,
        arguments.work_dir.resolve(),
    )
    completed = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    if completed.returncode != 0:
        print(f"PyInstaller failed with exit code {completed.returncode}.", file=sys.stderr)
        return completed.returncode

    problems = build_app_verify_embedded_tools(
        build_app_embedded_tools_root(dist_dir, arguments.name), hashes
    )
    if problems:
        print("BUILD VERIFICATION FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    output_kind = "macos-app" if sys.platform == "darwin" else "windows-folder"
    output_path = (
        dist_dir / f"{arguments.name}.app"
        if sys.platform == "darwin"
        else dist_dir / arguments.name
    )
    documents_dir = (
        output_path / "Contents" / "Resources" if output_kind == "macos-app" else output_path
    )
    documents_dir.mkdir(parents=True, exist_ok=True)
    license_name = _copy_project_license(documents_dir)
    if arguments.ffmpeg_license is not None:
        provider_licence = Path(arguments.ffmpeg_license).expanduser()
        if provider_licence.is_file():
            shutil.copy2(provider_licence, documents_dir / "FFMPEG-LICENSE.txt")
        else:
            print(
                f"Warning: --ffmpeg-license {provider_licence} does not exist.",
                file=sys.stderr,
            )
    notices = build_app_notices(
        tools_report, _build_app_provider(arguments.ffmpeg_bin, ffmpeg), arguments.ffmpeg_license
    )
    manifest = build_app_manifest(
        arguments.name,
        output_kind,
        output_path,
        tools_report,
        hashes,
        license_name,
        generated_utc,
    )
    manifest["runtime"] = runtime_report
    # PyInstaller rewrites Mach-O load paths and signatures, so packaged hashes
    # differ legitimately from the inputs. Record both identities explicitly.
    manifest["input_hashes"] = hashes
    embedded_root = build_app_embedded_tools_root(dist_dir, arguments.name)
    suffix = ".exe" if sys.platform == "win32" else ""
    manifest["hashes"] = {name: build_app_sha256(embedded_root / f"{name}{suffix}") for name in ("ffmpeg", "ffprobe")}
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (documents_dir / "BUILD-MANIFEST.json").write_text(payload, encoding="utf-8")
    (documents_dir / NOTICES_NAME).write_text(notices, encoding="utf-8")
    (output_path.parent / f"{output_path.name}.manifest.json").write_text(
        payload, encoding="utf-8"
    )

    if sys.platform == "darwin":
        try:
            build_app_dialog_helper(output_path)
        except (BuildError, OSError, subprocess.SubprocessError) as error:
            print(f"Startup reporter build failed; refusing this build: {error}", file=sys.stderr)
            return 1
        signing = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(output_path)], check=False
        )
        if signing.returncode != 0:
            print("Ad-hoc code signing failed; refusing this build.", file=sys.stderr)
            return 1

    print(f"Bundle: {output_path}")
    print(f"Manifest: {output_path.parent / (output_path.name + '.manifest.json')}")
    print("Now run the frozen self-test with a cleared PATH.")
    return 0


def _copy_project_license(documents_dir: Path) -> str | None:
    """Copy the optional project licence into the bundle without editing it."""

    licence = REPOSITORY_ROOT / "LICENSE.txt"
    if not licence.is_file():
        return None
    shutil.copy2(licence, documents_dir / licence.name)
    return licence.name


if __name__ == "__main__":
    raise SystemExit(build_app_main())
