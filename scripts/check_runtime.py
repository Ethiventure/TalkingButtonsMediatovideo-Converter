"""Probe a Python interpreter for Mediatovideo Converter compatibility.

Launchers call this once per candidate interpreter instead of embedding version
rules in shell or PowerShell. All policy (minimum Python/Tk versions, probe
timeout, error wording) lives in ``mediatovideo_converter/runtime.py``.

Examples:
    python3 scripts/check_runtime.py --check-runtime
    python3 scripts/check_runtime.py --check-runtime --python /opt/homebrew/bin/python3.14
    python3 scripts/check_runtime.py --print-requirements
    python3 scripts/check_runtime.py --check-runtime --python py --python-arg=-3.14

Exit codes: 0 supported, 1 unsupported, 2 usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

# Make the repository copy of the package win over any installed copy, so the
# checker always applies the policy that ships with this checkout.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mediatovideo_converter import runtime  # noqa: E402


def check_runtime_main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point for the runtime checker."""
    parser = argparse.ArgumentParser(
        prog="check_runtime", description="Verify that a Python interpreter can run the GUI."
    )
    parser.add_argument("--check-runtime", action="store_true", help="check the selected interpreter")
    parser.add_argument("--runtime-probe", action="store_true", help="run the probe in this process")
    parser.add_argument("--runtime-probe-report", metavar="PATH", default=None, help="write probe JSON here")
    parser.add_argument("--python", metavar="PATH", default=None, help="candidate interpreter to check")
    parser.add_argument("--python-arg", metavar="ARG", action="append", default=[], help="argument before -c")
    parser.add_argument("--timeout", metavar="SECONDS", type=float, default=None, help="probe time limit override")
    parser.add_argument("--print-requirements", action="store_true", help="print supported versions and exit")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.print_requirements:
        requirements = runtime.runtime_get_requirements()
        print("python>={} tk>={}".format(requirements["python_text"], requirements["tk_text"]))
        return 0
    if args.runtime_probe or args.runtime_probe_report:
        probe_args = (
            [runtime.RUNTIME_PROBE_REPORT_FLAG, args.runtime_probe_report]
            if args.runtime_probe_report
            else []
        )
        return runtime.runtime_probe_main(probe_args)
    try:
        payload = runtime.runtime_check(args.python, prefix_args=args.python_arg, timeout=args.timeout)
    except runtime.RuntimeUnsupportedError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        "OK: Compatible Python and Tkinter found\nPython: {} at {}\nTk: {}\n"
        "GUI widgets: Treeview and Progressbar created and drawn".format(
            payload.get("python_version"),
            payload.get("python_executable"),
            payload.get("tk_patchlevel"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(check_runtime_main())
