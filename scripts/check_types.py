#!/usr/bin/env python3
"""Type check of the integration (mypy) against the errors it already has.

The code has older type errors, mostly in annotations of Home Assistant's
attributes; they are listed in scripts/mypy-baseline.txt. A new one fails;
one that went away is reported, so that the baseline shrinks with the fix.
Errors are compared without line numbers: moving code is not a new error.

  scripts/check_types.py           check (on a GitHub runner: annotations)
  scripts/check_types.py --update  print the baseline of the code as it is
                                   (scripts/run_lint.sh <tag> --update > scripts/mypy-baseline.txt)

Runs inside the Home Assistant container (scripts/run_lint.sh): mypy reads
the types of the Home Assistant installed there.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "mypy-baseline.txt"
TARGET = "custom_components/haier_evo"
LINE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+): error: (?P<msg>.*?)(?:  \[(?P<code>[a-z-]+)\])?$")


def run_mypy() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--ignore-missing-imports", "--show-error-codes", "--no-error-summary",
         "--no-color-output", "--no-pretty", "--hide-error-context", "--cache-dir", os.environ.get("MYPY_CACHE_DIR", "/tmp/mypy"),
         TARGET],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def parse(output: str) -> list[dict]:
    """The errors of a mypy run: path, line, code, message and the key they are compared by."""
    out = []
    for line in output.splitlines():
        match = LINE.match(line.strip())
        if match:
            error = match.groupdict()
            error["code"] = error["code"] or "misc"
            error["key"] = f"{error['path']}: [{error['code']}] {error['msg']}"
            out.append(error)
    return out


def compare(errors: list[dict], baseline: list[str]) -> tuple[list[dict], list[str]]:
    """(new errors, baseline lines no longer found); the same error twice counts twice."""
    left = Counter(baseline)
    new = []
    for error in errors:
        if left[error["key"]] > 0:
            left[error["key"]] -= 1
        else:
            new.append(error)
    return new, sorted(left.elements())


def read_baseline(path: Path = BASELINE) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [line for line in lines if line.strip() and not line.startswith("#")]


def _data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def report(new: list[dict], gone: list[str], total: int, github: bool) -> list[str]:
    lines = []
    for error in new:
        if github:
            lines.append(f"::error file={_prop(error['path'])},line={error['line']},title={_prop('mypy [' + error['code'] + ']')}::"
                         + _data(error["msg"]))
        else:
            lines.append(f"{error['path']}:{error['line']}: new type error: {error['msg']}  [{error['code']}]")
    if gone:
        text = (f"{len(gone)} type errors of the baseline are gone: "
                "scripts/run_lint.sh <tag> --update > scripts/mypy-baseline.txt")
        lines.append(f"::notice title=mypy::{_data(text)}" if github else text)
    lines.append(f"mypy: {total} errors, {total - len(new)} of them known, {len(new)} new")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="print the baseline of the code as it is")
    args = parser.parse_args(argv)
    code, output = run_mypy()
    errors = parse(output)
    if code not in (0, 1) or (code == 1 and not errors):
        sys.stdout.write(output)
        print(f"mypy did not run through (exit code {code})")
        return 2
    if args.update:
        print("# mypy errors the code already has (scripts/check_types.py); a new one fails CI")
        print("\n".join(sorted(e["key"] for e in errors)))
        return 0
    new, gone = compare(errors, read_baseline())
    print("\n".join(report(new, gone, len(errors), bool(os.environ.get("GITHUB_ACTIONS")))))
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
