#!/bin/sh
# Run the checks (tests/) inside the Home Assistant container of one version:
#   scripts/run_checks.sh 2026.9.4      a release
#   scripts/run_checks.sh 2026.12.0b1   a beta
#   scripts/run_checks.sh dev           the nightly build
# The same image users run: its Python and its dependencies. CI calls it once
# per version (see scripts/ha_versions.py); it also works on any Docker host.
set -eu

tag="${1:?usage: scripts/run_checks.sh <Home Assistant container tag>}"
case "$tag" in
  "" | *[!A-Za-z0-9._-]*) echo "unexpected tag: $tag" >&2; exit 2 ;;
esac
image="ghcr.io/home-assistant/home-assistant:$tag"
cd "$(dirname "$0")/.."

docker pull --quiet "$image" >/dev/null
# the checkout is mounted read-only: the checks write to temporary directories only
docker run --rm --entrypoint sh \
  -v "$PWD:/repo:ro" -w /repo \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$image" -c '
    set -eu
    python3 -c "from homeassistant.const import __version__; print(\"Home Assistant\", __version__)"
    deps="websocket-client>=1.8.0,<2 pytest>=8.4"
    python3 -m pip install --quiet --disable-pip-version-check $deps 2>/dev/null \
      || uv pip install --system --quiet $deps
    python3 -m pytest -q -p no:cacheprovider tests
  '
