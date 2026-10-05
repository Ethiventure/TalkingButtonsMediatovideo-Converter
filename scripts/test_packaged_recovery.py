"""Validate offline repair using a disposable native app and generated media.

The installed app is never changed. A private home/cache and a copied package
let CI damage its own FFprobe, restore it through the packaged repair entry
point, and prove the restored runtime works with no host tools on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def test_packaged_recovery_executable(bundle: Path) -> Path:
    """Locate the native entry point for the current operating system."""
    if sys.platform == "darwin":
        return bundle / "Contents" / "MacOS" / "Mediatovideo Converter"
    if sys.platform == "win32":
        return bundle / "Mediatovideo Converter.exe"
    raise RuntimeError("Packaged recovery tests require native macOS or Windows.")


def test_packaged_recovery_main() -> int:
    """Run the native damaged-package scenario and preserve its evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    reports = args.report_dir.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {"ok": False, "platform": sys.platform}
    with tempfile.TemporaryDirectory(prefix="mediatovideo-native-repair-") as temporary:
        fixture = Path(temporary)
        # Spaces and apostrophes exercise updater path transport, not just the
        # easy CI path. The helper must never evaluate a user path as script.
        target = fixture / "repair's test folder" / args.bundle.name
        target.parent.mkdir()
        home = fixture / "private-home"
        home.mkdir()
        scratch = fixture / "temp"
        scratch.mkdir()
        environment = {"PATH": "", "HOME": str(home), "USERPROFILE": str(home),
                       "LOCALAPPDATA": str(home / "AppData" / "Local"),
                       "TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch)}
        if os.name == "nt":
            environment["SystemRoot"] = os.environ["SystemRoot"]
        try:
            if sys.platform == "darwin":
                subprocess.run(["/usr/bin/ditto", "--norsrc", "--noextattr",
                                str(args.bundle.resolve()), str(target)], check=True, timeout=120)
                tools = target / "Contents" / "Frameworks" / "video_tools"
                probe = tools / "ffprobe"
            else:
                shutil.copytree(args.bundle.resolve(), target)
                tools = target / "_internal" / "video_tools"
                probe = tools / "ffprobe.exe"
            digest = hashlib.sha256(probe.read_bytes()).hexdigest()
            executable = test_packaged_recovery_executable(target)
            seed = subprocess.run([str(executable), "--prepare-recovery"], env=environment,
                                  timeout=180, capture_output=True)
            assert seed.returncode == 0, "Healthy packaged recovery preparation failed"
            probe.rename(probe.with_name(probe.name + ".unavailable"))
            started = reports / "repair-start.json"
            started.unlink(missing_ok=True)
            repair = subprocess.run([str(executable), "--repair-report", str(started),
                                     "--repair-no-relaunch"], env=environment,
                                    timeout=180, capture_output=True)
            launch = json.loads(started.read_text(encoding="utf-8"))
            evidence["launch"] = launch
            assert repair.returncode == 0 and launch.get("ok") is True, launch
            result_file = Path(str(launch["result_path"]))
            deadline = time.monotonic() + 100
            result = None
            while time.monotonic() < deadline:
                try:
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    time.sleep(0.25)
            assert isinstance(result, dict), "Detached updater did not write its result"
            evidence["replacement"] = result
            assert result.get("ok") is True, result
            backup = Path(str(launch["backup_path"]))
            retained_probe = backup / probe.relative_to(target)
            assert retained_probe.with_name(retained_probe.name + ".unavailable").is_file(), \
                "The original damaged package was not preserved in its backup"
            assert probe.is_file(), "FFprobe was not restored"
            assert hashlib.sha256(probe.read_bytes()).hexdigest() == digest, "Restored FFprobe differs"
            assert not probe.with_name(probe.name + ".unavailable").exists(), "Damaged tree was not replaced"
            if sys.platform == "darwin":
                subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)],
                               check=True, timeout=60, capture_output=True)
            final_report = reports / "repaired-selftest.json"
            final_report.unlink(missing_ok=True)
            final = subprocess.run([str(executable), "--self-test", "--self-test-report", str(final_report)],
                                   env=environment, timeout=90, capture_output=True)
            payload = json.loads(final_report.read_text(encoding="utf-8"))
            assert final.returncode == 0 and payload.get("ok") is True, payload
            assert payload.get("frozen") is True and not payload.get("path"), payload
            evidence.update(ok=True, restored_ffprobe_sha256=digest, empty_path_verified=True)
        except Exception as error:
            evidence["error"] = f"{type(error).__name__}: {error}"
            print(evidence["error"], file=sys.stderr)
        finally:
            # Retain detached-helper startup errors as well as application logs:
            # parameter binding can fail before the helper writes its result.
            for index, log in enumerate(home.rglob("*helper-startup.log")):
                shutil.copy2(log, reports / f"helper-startup-{index}.log")
            for index, log in enumerate(home.rglob("application-debug.log")):
                shutil.copy2(log, reports / f"repair-debug-{index}.log")
            for index, log in enumerate(scratch.rglob("application-debug.log")):
                shutil.copy2(log, reports / f"repair-temp-debug-{index}.log")
            (reports / "native-recovery.json").write_text(
                json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print("Native recovery:", "passed" if evidence["ok"] else "failed", reports)
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(test_packaged_recovery_main())
