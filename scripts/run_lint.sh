#!/bin/sh
# ruff (the rules of pyproject.toml) and mypy against its baseline
# (scripts/check_types.py), inside the Home Assistant container of one
# version: mypy reads the types of the Home Assistant installed there.
#   scripts/run_lint.sh 2026.11.1
#   scripts/run_lint.sh 2026.11.1 --update > scripts/mypy-baseline.txt
set -eu

tag="${1:?usage: scripts/run_lint.sh <Home Assistant container tag> [--update]}"
case "$tag" in
  "" | *[!A-Za-z0-9._-]*) echo "unexpected tag: $tag" >&2; exit 2 ;;
esac
mode="${2:-check}"
case "$mode" in
  check | --update) ;;
  *) echo "unexpected argument: $mode" >&2; exit 2 ;;
esac
image="ghcr.io/home-assistant/home-assistant:$tag"
cd "$(dirname "$0")/.."

docker pull --quiet "$image" >/dev/null
# the versions are pinned (requirements_test.txt): a new ruff or mypy finds new things by itself
docker run --rm --entrypoint sh \
  -v "$PWD:/repo:ro" -w /repo \
  -e PYTHONDONTWRITEBYTECODE=1 -e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy \
  -e GITHUB_ACTIONS="${GITHUB_ACTIONS:-}" -e MODE="$mode" \
  "$image" -c '
    set -u
    deps=$(grep -E "^(ruff|mypy)==" requirements_test.txt)
    python3 -m pip install --quiet --disable-pip-version-check $deps >/dev/null 2>&1 \
      || uv pip install --system --quiet $deps >/dev/null
    if [ "$MODE" = --update ]; then
      exec python3 scripts/check_types.py --update
    fi
    status=0
    format=concise
    if [ -n "$GITHUB_ACTIONS" ]; then format=github; fi
    echo "Home Assistant $(python3 -c "from homeassistant.const import __version__; print(__version__)"), $(ruff --version), $(python3 -m mypy --version)"
    # ruff names files by their path in the container: annotations need it from the checkout
    if ruff check --output-format="$format" . > /tmp/ruff.txt; then
      echo "ruff: no errors"
    else
      status=1
    fi
    sed "s#/repo/##g" /tmp/ruff.txt
    python3 scripts/check_types.py || status=1
    exit $status
  '
