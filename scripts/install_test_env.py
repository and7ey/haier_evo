#!/usr/bin/env python3
"""Install the test harness for the Home Assistant that is installed here.

  python3 scripts/install_test_env.py              the Home Assistant installed
  python3 scripts/install_test_env.py 2026.9.4     that release (installed too)

The tests need pytest-homeassistant-custom-component, Home Assistant's own test
harness, which pins one release: homeassistant==<release>. The harness of a
release is pinned in scripts/harness_pins.txt, so runs are reproducible; for a
release not listed there the script takes the newest harness that pins this
release or an older one (and says so). It installs the harness
with this Home Assistant and the packages it depends on kept (uv's
--override): in the Home Assistant container of a version
(scripts/run_checks.sh) the core stays what users run, the harness brings its
test packages.

A release without a harness of its own yet (a new release, a beta, the
nightly build) runs on the previous harness. Two of its incompatibilities with
Home Assistant 2026.11 are patched here, only where they show up:
 - 2026.11 aliases httpx to httpx2 and refuses to start once httpx is
   imported, which the old harness does first: the alias is made at
   interpreter start (a .pth file);
 - _clear_logger_overwrites moved to homeassistant.components.logger.helpers.

Needs uv (the Home Assistant container has it) and PyPI.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
from pathlib import Path

PLUGIN = "pytest-homeassistant-custom-component"
PINS = Path(__file__).resolve().parent / "harness_pins.txt"
PYPI = "https://pypi.org/pypi/{}/json"
ROOT = Path(__file__).resolve().parent.parent
# 2026.9.4, 2026.12.0b1, 2026.12.0.dev20261112 (the nightly build)
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:b(\d+)|\.dev(\d+))?$")


def version_key(version: str) -> tuple[int, ...] | None:
    """Home Assistant versions in their order: a nightly build, then the betas, then the release."""
    match = VERSION.match(version.strip())
    if not match:
        return None
    year, month, patch, beta, dev = match.groups()
    stage = (2, 0) if beta is None and dev is None else (1, int(beta)) if beta is not None else (0, int(dev))
    return int(year), int(month), int(patch), *stage


def pypi(path: str) -> dict:
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(PYPI.format(path), timeout=30) as resp:  # noqa: S310 - fixed https URL
                return json.load(resp)
        except (OSError, ValueError) as err:
            if attempt == 3:
                raise SystemExit(f"PyPI did not answer ({err!r})") from err
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def installed_homeassistant() -> str | None:
    try:
        from importlib.metadata import version

        return version("homeassistant")
    except Exception:  # noqa: BLE001 - not installed
        return None


def pinned(release: str) -> str | None:
    """The Home Assistant release a harness release pins."""
    for requirement in pypi(f"{PLUGIN}/{release}").get("info", {}).get("requires_dist") or []:
        name, _, version = requirement.partition("==")
        if name.strip() == "homeassistant" and version:
            return version.split(";")[0].strip()
    return None


def pinned_harness(target: str, path: Path = PINS) -> tuple[str, str] | None:
    """(harness release, the Home Assistant it pins) of scripts/harness_pins.txt, if listed."""
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) == 3 and fields[0] == target:
            return fields[1], fields[2]
    return None


def harness_for(target: str) -> tuple[str, str]:
    """(harness release, the Home Assistant it pins): the newest one not newer than target."""
    want = version_key(target)
    if want is None:
        raise SystemExit(f"unexpected Home Assistant version: {target}")
    releases = pypi(PLUGIN).get("releases") or {}
    oldest = sorted(
        (v for v, files in releases.items() if files and not all(f.get("yanked") for f in files)),
        key=lambda v: min(f["upload_time_iso_8601"] for f in releases[v]),
    )
    pins: dict[str, str | None] = {}

    def key(index: int) -> tuple[int, ...] | None:
        release = oldest[index]
        if release not in pins:
            pins[release] = pinned(release)
        return version_key(pins[release]) if pins[release] else None

    def fits(index: int) -> bool:
        # a release with no pin read counts as the nearest older one that has it
        for earlier in range(index, max(index - 5, -1), -1):
            found = key(earlier)
            if found is not None:
                return found <= want
        return False

    # each harness release pins a newer Home Assistant than the one before it:
    # the last release that fits is found in about ten requests, however old
    # target is (the minimum of hacs.json stays put for months)
    low, high = 0, len(oldest)
    while low < high:
        middle = (low + high) // 2
        if fits(middle):
            low = middle + 1
        else:
            high = middle
    for index in range(low - 1, max(low - 6, -1), -1):
        found = key(index)
        if found is not None and found <= want:
            return oldest[index], pins[oldest[index]]  # type: ignore[return-value]
    raise SystemExit(f"no {PLUGIN} for Home Assistant {target} or older")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def kept(target: str) -> dict[str, str]:
    """What the harness must not change: Home Assistant, and with the one of target installed, the
    packages it depends on. An older harness pins the libraries of its own release (sqlalchemy, for
    one); the Home Assistant of the image runs on its own. Test packages a previous run installed
    are not kept: a newer harness brings its own."""
    from importlib.metadata import distributions

    if installed_homeassistant() != target:
        return {"homeassistant": target}
    installed = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if name and dist.version:
            installed.setdefault(_normalized(name), dist)
    packages: dict[str, str] = {}
    todo = ["homeassistant"]
    while todo:
        name = todo.pop()
        dist = installed.get(name)
        if dist is None or name in packages:
            continue
        packages[name] = dist.version
        for requirement in dist.requires or []:
            if re.search(r"\bextra\s*==", requirement.partition(";")[2]):
                continue  # an optional dependency
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                todo.append(_normalized(match.group(1)))
    return packages


def install(target: str, release: str) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is needed: python3 -m pip install uv")
    system = ["--system"] if sys.prefix == sys.base_prefix and not os.environ.get("VIRTUAL_ENV") else []
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as file:
        file.write("".join(f"{name}=={version}\n" for name, version in kept(target).items()))
    try:
        # the Home Assistant container lists its own wheel index first; the
        # harness pins versions only PyPI has: look at both indexes
        subprocess.run(
            [uv, "pip", "install", *system, "--python", sys.executable, "--quiet", "--override", file.name,
             "--index-strategy", "unsafe-best-match",
             f"{PLUGIN}=={release}", "-r", str(ROOT / "requirements_test.txt")],
            check=True,
        )
    finally:
        os.unlink(file.name)


LOGGER_MODULES = ("homeassistant.components.logger", "homeassistant.components.logger.helpers")


def _module_with(name: str, modules: tuple[str, ...]) -> str | None:
    """The first of modules that has name, imported in a new interpreter."""
    code = (
        "import importlib, sys\n"
        "for module in sys.argv[2:]:\n"
        "    try:\n"
        "        if hasattr(importlib.import_module(module), sys.argv[1]):\n"
        "            print(module); break\n"
        "    except ImportError:\n"
        "        pass\n"
    )
    out = subprocess.run([sys.executable, "-c", code, name, *modules], capture_output=True, text=True, check=False)
    return out.stdout.strip() or None


def patch_for_newer_homeassistant() -> list[str]:
    """What an older harness needs on a newer Home Assistant; nothing when it fits."""
    done = []
    site = Path(sysconfig.get_paths()["purelib"])
    httpx2 = importlib.util.find_spec("httpx2")
    if httpx2 is not None:
        import httpx2 as module  # noqa: PLC0415

        if hasattr(module, "alias_httpx"):
            (site / "zz_haier_evo_alias_httpx.pth").write_text("import httpx2; httpx2.alias_httpx()\n")
            done.append("httpx is an alias of httpx2 from interpreter start")
    common = site / "pytest_homeassistant_custom_component" / "common.py"
    old = "    SERVICE_SET_LEVEL,\n    _clear_logger_overwrites,\n)"
    text = common.read_text(encoding="utf-8")
    if old in text:
        # where this Home Assistant defines it; asked of a new interpreter (the
        # .pth above applies to it), by import: an image may ship no sources
        home = _module_with("_clear_logger_overwrites", LOGGER_MODULES)
        if home and home != LOGGER_MODULES[0]:
            new = f"    SERVICE_SET_LEVEL,\n)\nfrom {home} import _clear_logger_overwrites"
            common.write_text(text.replace(old, new), encoding="utf-8")
            done.append(f"_clear_logger_overwrites imported from {home}")
    return done


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else installed_homeassistant()
    if not target:
        raise SystemExit("Home Assistant is not installed here: name the release, e.g. 2026.9.4")
    if found := pinned_harness(target):
        release, pin = found
        print(f"Home Assistant {target}: {PLUGIN} {release} (pins {pin}), from {PINS.name}")
    else:
        release, pin = harness_for(target)
        print(f"Home Assistant {target}: {PLUGIN} {release} (pins {pin}), newest that fits: "
              f"not in {PINS.name}, add it there to keep the runs the same")
    install(target, release)
    if pin != target:
        for line in patch_for_newer_homeassistant():
            print(f"  patched: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
