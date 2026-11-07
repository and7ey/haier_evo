#!/usr/bin/env python3
"""Home Assistant versions the checks run against, worked out from PyPI.

The window moves by itself, nothing in the repository has to be changed:

- prev2, prev1: the two releases before the current one (their last patch);
- current: the newest release (its last patch);
- next: the release after it, as its newest beta while there is one,
  else the nightly build (container tag "dev");
- minimum: the minimum version of hacs.json (its last patch) while the window
  no longer covers it, else empty: what HACS promises is always checked.

Every value is a tag of ghcr.io/home-assistant/home-assistant. Prints lines
for $GITHUB_OUTPUT: one per version and "matrix", the jobs to run (a version
without a tag has no job); --summary prints a Markdown table for
$GITHUB_STEP_SUMMARY instead.
Standard library only: runs on the runner's own Python.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

PYPI_URL = "https://pypi.org/pypi/homeassistant/json"
VERSION = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)(?:b(\d+))?$")
CHANNELS = ("minimum", "prev2", "prev1", "current", "next")
TITLES = {
    "minimum": "минимальная из hacs.json",
    "prev2": "позапрошлая",
    "prev1": "прошлая",
    "current": "текущая",
    "next": "следующая (не блокирует)",
}
# the names of the CI jobs: "Home Assistant 2026.9.4 (previous but one)"
JOB_NAMES = {
    "minimum": "minimum of hacs.json",
    "prev2": "previous but one",
    "prev1": "previous",
    "current": "current",
    "next": "next, does not block",
}
NON_BLOCKING = ("next",)


def fetch_pypi(url: str = PYPI_URL, attempts: int = 3) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
                return json.load(resp)
        except (OSError, ValueError) as err:
            if attempt == attempts:
                raise SystemExit(f"PyPI did not answer ({err!r}): the versions cannot be worked out") from err
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def releases(pypi: dict) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
    """({(year, month): last patch}, {(year, month): newest beta of .0}) of what is not yanked."""
    finals: dict[tuple[int, int], int] = {}
    betas: dict[tuple[int, int], int] = {}
    for version, files in (pypi.get("releases") or {}).items():
        match = VERSION.match(version)
        if not match or not files or all(f.get("yanked") for f in files):
            continue
        year, month, patch, beta = (int(g) if g is not None else None for g in match.groups())
        if beta is None:
            finals[(year, month)] = max(patch, finals.get((year, month), -1))
        elif patch == 0:
            betas[(year, month)] = max(beta, betas.get((year, month), -1))
    return finals, betas


def following(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def window(pypi: dict, hacs_minimum: str | None = None) -> dict[str, str]:
    finals, betas = releases(pypi)
    minors = sorted(finals)
    if len(minors) < 3:
        raise SystemExit("PyPI lists fewer than three Home Assistant releases")
    tag = {m: f"{m[0]}.{m[1]}.{finals[m]}" for m in minors}
    prev2, prev1, current = minors[-3:]
    nxt = following(*current)
    result = {
        "prev2": tag[prev2],
        "prev1": tag[prev1],
        "current": tag[current],
        "next": f"{nxt[0]}.{nxt[1]}.0b{betas[nxt]}" if nxt in betas else "dev",
        "minimum": "",
    }
    if hacs_minimum:
        match = VERSION.match(hacs_minimum.strip())
        if not match or match.group(4) is not None:
            raise SystemExit(f"hacs.json: unexpected minimum version {hacs_minimum!r}")
        minimum = (int(match.group(1)), int(match.group(2)))
        if minimum not in (prev2, prev1, current):
            result["minimum"] = tag.get(minimum, hacs_minimum.strip())
    return result


def summary(versions: dict[str, str]) -> str:
    lines = ["| Шаг | Версия Home Assistant |", "|---|---|"]
    lines += [f"| {TITLES[key]} | `{versions[key] or 'входит в окно'}` |" for key in CHANNELS]
    return "\n".join(lines) + "\n"


def matrix(versions: dict[str, str]) -> list[dict]:
    """The CI jobs, one per version with a tag, in the order of CHANNELS."""
    return [
        {"channel": key, "tag": versions[key], "name": JOB_NAMES[key], "blocking": key not in NON_BLOCKING}
        for key in CHANNELS if versions[key]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pypi", help="a saved PyPI answer instead of asking PyPI")
    parser.add_argument("--hacs", default="hacs.json", help="hacs.json with the minimum version")
    parser.add_argument("--summary", action="store_true", help="print a Markdown table instead")
    args = parser.parse_args(argv)
    if args.pypi:
        with open(args.pypi, encoding="utf-8") as file:
            pypi = json.load(file)
    else:
        pypi = fetch_pypi()
    hacs_minimum = None
    if args.hacs:
        with open(args.hacs, encoding="utf-8") as file:
            hacs_minimum = json.load(file).get("homeassistant")
    versions = window(pypi, hacs_minimum)
    if args.summary:
        sys.stdout.write(summary(versions))
    else:
        sys.stdout.write("".join(f"{key}={versions[key]}\n" for key in CHANNELS))
        sys.stdout.write(f"matrix={json.dumps(matrix(versions))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
