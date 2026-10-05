"""Run the unified regression suite and optional real GUI/video smoke test."""

from __future__ import annotations

import argparse
import subprocess
import sys
import unittest
from pathlib import Path


def test_harness_main() -> int:
    """Run all test modules; real runtime checks are explicit and time bounded."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--integration', action='store_true', help='Also exercise the actual GUI and video tools.')
    parser.add_argument('--package', type=Path, help='Also restore a disposable damaged native package.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    success = unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    if args.integration:
        # Run even after unit failures so one module cannot mask integration errors.
        try:
            result = subprocess.run([sys.executable, str(root / 'run_app.py'), '--self-test', '--self-test-report', str(root / 'build' / 'self-test.json')], cwd=root, timeout=90)
            success = (result.returncode == 0) and success
        except subprocess.TimeoutExpired:
            print('Integration smoke test timed out after 90 seconds.', file=sys.stderr)
            success = False
    if args.package:
        try:
            result = subprocess.run([
                sys.executable, str(root / 'scripts' / 'test_packaged_recovery.py'),
                '--bundle', str(args.package.resolve()),
                '--report-dir', str(root / 'build' / 'smoke-diagnostics' / 'recovery'),
            ], cwd=root, timeout=600)
            success = (result.returncode == 0) and success
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f'Native recovery test failed: {error}', file=sys.stderr)
            success = False
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(test_harness_main())
