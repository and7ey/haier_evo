#!/usr/bin/env python3
"""Results of the checks in CI: job summaries, annotations and the README badges.

The reports come from scripts/run_checks.sh (CHECKS_REPORT, JSON lines, see
tests/ci_report.py), one per version: <channel>.jsonl.

  ci_results.py job --report F --channel C --tag T --outcome O --summary-file S
      in the job of one version: adds the outcome of its checks step to the
      report, writes the summary of the job (the image, the versions inside
      it, every script with its counts and time, what failed) to S and prints
      annotations (workflow commands) on stdout.
  ci_results.py run --reports DIR [--summary | --verdict]
      in the results job, with the versions of the run in HA_VERSIONS (the
      outputs of scripts/ha_versions.py as JSON): prints badges=<JSON> for
      $GITHUB_OUTPUT, the files of the badges branch ({file name: badge in
      the shields.io endpoint format}): tests.json and one ha-<channel>.json
      per version, ha-minimum.json with the minimum of hacs.json; --summary
      prints the table of the run; --verdict exits 1 when a version that
      blocks did not pass.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_versions import CHANNELS, JOB_NAMES, NON_BLOCKING, TITLES  # noqa: E402

DEV = re.compile(r"^(\d{4}\.\d{1,2})\.\d+\.dev")
RELEASE = re.compile(r"^(\d{4}\.\d{1,2})\.\d")     # 2026.9.4 -> 2026.9
MINIMUM = re.compile(r"^\d{4}\.\d{1,2}\.\d+$")
PASSED, FAILED, ERROR = "passed", "failed", "error"
MAX_ANNOTATIONS = 10   # GitHub shows 10 errors and 10 warnings per step
META = ("homeassistant", "python", "pytest", "plugin", "image", "digest")


# ---------------------------------------------------------------- reports

def read_report(path: Path) -> dict | None:
    """What one report says; None without one (the step did not get to the checks)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    report = {"meta": {}, "scripts": [], "passed": 0, "failed": 0, "broken": 0, "outcome": None}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if "script" in record:
                script = {
                    "script": str(record["script"]),
                    "passed": int(record["passed"]),
                    "failed": int(record["failed"]),
                    "ok": bool(record["ok"]),
                    "seconds": float(record.get("seconds") or 0),
                    "failures": [str(f) for f in record.get("failures") or ()],
                }
                report["scripts"].append(script)
                report["passed"] += script["passed"]
                report["failed"] += script["failed"]
                report["broken"] += not script["ok"]
            elif "outcome" in record:
                report["outcome"] = record["outcome"]
            else:
                report["meta"].update({k: record[k] for k in META if k in record})
        except (ValueError, KeyError, TypeError, AttributeError):
            report["broken"] += 1
    return report


def entry(channel: str, tag: str, report: dict | None) -> dict:
    """One version: its report and what came of it."""
    out = {"channel": channel, "tag": tag, "meta": {}, "scripts": [], "passed": 0, "failed": 0, "seconds": 0.0}
    if report is None or not report["scripts"]:
        status = ERROR
        if report is not None:
            out["meta"] = report["meta"]
    else:
        out.update(meta=report["meta"], scripts=report["scripts"], passed=report["passed"], failed=report["failed"],
                   seconds=round(sum(s["seconds"] for s in report["scripts"]), 1))
        if report["failed"] or report["broken"]:
            status = FAILED
        elif report["outcome"] not in (None, "success"):
            status = ERROR      # failed outside the checks
        else:
            status = PASSED
    out["status"] = status
    out["homeassistant"] = out["meta"].get("homeassistant")
    return out


def results(versions: dict[str, str], reports: Path) -> list[dict]:
    """One entry per version of the run, in the order of the jobs."""
    return [entry(c, versions[c], read_report(reports / f"{c}.jsonl")) for c in CHANNELS if versions.get(c)]


def blocking(e: dict) -> bool:
    return e["channel"] not in NON_BLOCKING


def version_name(e: dict) -> str:
    """The tag, but the nightly build as "2026.12-dev": the release it leads to."""
    if e["tag"] == "dev":
        match = DEV.match(e["homeassistant"] or "")
        if match:
            return f"{match.group(1)}-dev"
    return e["tag"]


def short_digest(e: dict) -> str:
    digest = e["meta"].get("digest") or ""
    return digest[:19] if digest.startswith("sha256:") else ""


# ---------------------------------------------------------------- badges

def badge(label: str, message: str, color: str, **extra) -> dict:
    return {"schemaVersion": 1, "label": label, "message": message, "color": color, **extra}


def tests_badge(entries: list[dict]) -> dict:
    """Checks passed: on the current version; on a failure, the worst blocking version."""
    blocks = [e for e in entries if blocking(e)]
    if not blocks:
        return badge("tests", "no results", "lightgrey")
    failed = [e for e in blocks if e["status"] == FAILED]
    if failed:
        worst = max(failed, key=lambda e: e["failed"])
        return badge("tests", f"{worst['passed']} passed, {max(worst['failed'], 1)} failed", "red")
    if any(e["status"] == ERROR for e in blocks):
        return badge("tests", "error", "red")
    shown = next((e for e in blocks if e["channel"] == "current"), blocks[-1])
    return badge("tests", f"{shown['passed']} passed", "brightgreen")


def _ha_badge(message: str, color: str) -> dict:
    return badge("Home Assistant", message, color, namedLogo="homeassistant")


def _color(e: dict) -> str:
    if e["status"] == PASSED:
        return "brightgreen"
    return "red" if blocking(e) else "yellow"


def _mark(e: dict) -> str:
    return "" if e["status"] == PASSED else " ✗"


def version_badge(e: dict) -> dict:
    """One version: green when its checks passed, else a cross, red (yellow for next)."""
    return _ha_badge(version_name(e) + _mark(e), _color(e))


def release(tag: str) -> str | None:
    match = RELEASE.match(tag or "")
    return match.group(1) if match else None


def minimum_badge(entries: list[dict], minimum: str) -> dict:
    """The minimum of hacs.json, coloured by the check of its release: its own job
    once the window has left it behind, else the job of the window on that release."""
    checked = next((e for e in entries if e["channel"] == "minimum"), None) or next(
        (e for e in entries if blocking(e) and release(e["tag"]) == release(minimum)), None)
    if checked is None:
        return _ha_badge(f"≥ {minimum}", "blue")
    return _ha_badge(f"≥ {minimum}" + _mark(checked), _color(checked))


def badges(entries: list[dict], minimum: str | None = None) -> dict[str, dict]:
    """The files of the badges branch: tests.json, ha-minimum.json, one ha-<channel>.json per version."""
    out = {"tests.json": tests_badge(entries)}
    if minimum:
        out["ha-minimum.json"] = minimum_badge(entries, minimum)
    for e in entries:
        if e["channel"] != "minimum":
            out[f"ha-{e['channel']}.json"] = version_badge(e)
    return out


def hacs_minimum(path: str) -> str | None:
    """The minimum Home Assistant of hacs.json; None without one."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")).get("homeassistant")
    except (OSError, ValueError, AttributeError):
        return None
    value = str(value or "").strip()
    return value if MINIMUM.match(value) else None


# ---------------------------------------------------------------- summaries

def verdict(e: dict) -> str:
    if e["status"] == PASSED:
        return "✅ пройдено"
    mark = "❌" if blocking(e) else "⚠️"
    text = ("не пройдено" if e["status"] == FAILED
            else "ошибка вне проверок" if e["scripts"] else "не запустилось")
    return f"{mark} {text}" + ("" if blocking(e) else " (не блокирует)")


def run_summary(entries: list[dict]) -> str:
    lines = [
        "### Проверки по версиям Home Assistant",
        "",
        "| Шаг | Версия Home Assistant | Python | Образ | Пройдено | Не пройдено | Время, с | Итог |",
        "|---|---|---|---|---:|---:|---:|---|",
    ]
    for e in entries:
        version = f"`{e['tag']}`"
        if e["homeassistant"] and e["homeassistant"] != e["tag"]:
            version += f" ({e['homeassistant']})"
        ran = e["status"] != ERROR
        lines.append(" | ".join([
            f"| {TITLES[e['channel']]}", version, e["meta"].get("python") or "—",
            f"`{short_digest(e)}`" if short_digest(e) else "—",
            str(e["passed"]) if ran else "—", str(e["failed"]) if ran else "—",
            f"{e['seconds']:.0f}" if ran else "—", verdict(e) + " |",
        ]))
    return "\n".join(lines) + "\n"


def run_meta() -> list[str]:
    """The commit and the event of the run, from the environment of a GitHub runner."""
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    if not sha:
        return []
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", "")
    return [f"| Коммит | `{sha}` · {event} · `{branch}` |"]


def job_summary(e: dict) -> str:
    title = f"Home Assistant {e['tag']} ({JOB_NAMES[e['channel']]})"
    meta = e["meta"]
    if e["status"] == ERROR:
        head = f"### {verdict(e)}: {title}"
        head += ("\n\nПроверки прошли, но шаг Checks завершился с ошибкой. Причина — в его логе." if e["scripts"] else
                 "\n\nПроверки не дошли до конца: не скачался образ, не поставились зависимости или упал pytest. "
                 "Причина — в логе шага Checks.")
    else:
        head = f"### {verdict(e)}: {title} — {e['passed']} из {e['passed'] + e['failed']} проверок"
    rows = ["| | |", "|---|---|"]
    if meta.get("image"):
        rows.append(f"| Образ | `{meta['image']}` |")
    if meta.get("digest"):
        rows.append(f"| Digest | `{meta['digest']}` |")
    for key, name in (("homeassistant", "Home Assistant"), ("python", "Python"),
                      ("pytest", "pytest"), ("plugin", "pytest-homeassistant-custom-component")):
        if meta.get(key):
            rows.append(f"| {name} | `{meta[key]}` |")
    rows += run_meta()
    if e["scripts"]:
        rows.append(f"| Время проверок | {e['seconds']:.0f} с |")
    parts = [head, "", *rows] if len(rows) > 2 else [head]
    if e["scripts"]:
        parts += ["", "| Файл | Пройдено | Не пройдено | Время, с |", "|---|---:|---:|---:|"]
        for s in e["scripts"]:
            mark = "✅" if s["ok"] and not s["failed"] else "❌"
            parts.append(f"| {mark} `{s['script']}` | {s['passed']} | {s['failed']} | {s['seconds']:.1f} |")
    failing = [s for s in e["scripts"] if s["failures"]]
    if failing:
        parts += ["", "#### Что не прошло"]
        for s in failing:
            more = s["failed"] - len(s["failures"])
            body = "\n\n".join(s["failures"]).replace("```", "'''")
            parts += ["", f"<details open><summary><code>{s['script']}</code>: {s['failed']}</summary>", "",
                      "```text", body, "```", *([f"и ещё {more}"] if more > 0 else []), "</details>"]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- annotations

def _data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def command(level: str, message: str, title: str, file: str | None = None) -> str:
    props = ([f"file={_prop(file)}"] if file else []) + [f"title={_prop(title)}"]
    return f"::{level} {','.join(props)}::{_data(message)}"


def annotations(e: dict) -> list[str]:
    """Workflow commands: every failure (errors; warnings for a version that does not block), else a notice."""
    level = "error" if blocking(e) else "warning"
    where = f"Home Assistant {version_name(e)}"
    if e["status"] == PASSED:
        python = e["meta"].get("python")
        return [command("notice", f"{e['passed']} проверок пройдено" + (f", Python {python}" if python else "")
                        + f", {e['seconds']:.0f} с", where)]
    if e["status"] == ERROR and not e["failed"]:
        return [command(level, "Проверки не дошли до конца: причина в логе шага Checks", where)]
    out = []
    failures = [(s["script"], f) for s in e["scripts"] for f in s["failures"]]
    for script, text in failures[:MAX_ANNOTATIONS - 1]:
        out.append(command(level, text, f"{where}: {script}", f"tests/{script}"))
    rest = e["failed"] - len(out)
    if rest > 0:
        out.append(command(level, f"ещё {rest} не пройдено, все — в сводке задачи", where))
    return out


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    job = sub.add_parser("job", help="the summary and annotations of the job of one version")
    job.add_argument("--report", required=True)
    job.add_argument("--channel", required=True, choices=CHANNELS)
    job.add_argument("--tag", required=True)
    job.add_argument("--outcome", default="", help="steps.<id>.outcome of the checks step")
    job.add_argument("--summary-file", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    run = sub.add_parser("run", help="the badges, the table or the verdict of the run")
    run.add_argument("--reports", required=True, help="directory with <channel>.jsonl")
    run.add_argument("--versions", default=os.environ.get("HA_VERSIONS", ""),
                     help="JSON of the versions (default: $HA_VERSIONS)")
    run.add_argument("--hacs", default="hacs.json", help="hacs.json with the minimum version")
    what = run.add_mutually_exclusive_group()
    what.add_argument("--summary", action="store_true", help="print the table of the run")
    what.add_argument("--verdict", action="store_true", help="exit 1 when a version that blocks did not pass")
    args = parser.parse_args(argv)

    if args.mode == "job":
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"outcome": args.outcome or None}) + "\n")
        e = entry(args.channel, args.tag, read_report(report))
        if args.summary_file:
            with open(args.summary_file, "a", encoding="utf-8") as file:
                file.write(job_summary(e))
        else:
            sys.stdout.write(job_summary(e))
        sys.stdout.write("".join(line + "\n" for line in annotations(e)))
        return 0

    if not args.versions:
        parser.error("no versions: pass --versions or set HA_VERSIONS")
    entries = results(json.loads(args.versions), Path(args.reports))
    if args.summary:
        sys.stdout.write(run_summary(entries))
    elif args.verdict:
        bad = [e for e in entries if blocking(e) and e["status"] != PASSED]
        for e in entries:
            print(f"Home Assistant {e['tag']} ({JOB_NAMES[e['channel']]}): {e['status']}")
        return 1 if bad or not entries else 0
    else:
        sys.stdout.write(f"badges={json.dumps(badges(entries, hacs_minimum(args.hacs)))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
