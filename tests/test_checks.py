"""pytest entry point: every check_*.py script runs in its own interpreter.

The scripts print one line per check (OK / FAIL) and exit non-zero on a
failure; they also run by hand: python tests/check_louvers.py
"""
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(p.name for p in HERE.glob("check_*.py"))


@pytest.mark.parametrize("script", SCRIPTS)
def test_check_script(script: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(HERE / script)],
        cwd=HERE.parent, capture_output=True, text=True, timeout=600,
    )
    output = proc.stdout + proc.stderr
    failed = [line for line in output.splitlines() if line.startswith("FAIL")]
    assert proc.returncode == 0 and "FAILS: 0" in output, "\n".join(failed) or output[-4000:]
