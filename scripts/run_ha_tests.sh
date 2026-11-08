#!/bin/sh
# Run the tests on a real Home Assistant core (tests_ha/) inside the Home
# Assistant container of the release that pytest-homeassistant-custom-component
# pins (requirements_test_ha.txt):
#   scripts/run_ha_tests.sh 2026.10.4
# The plugin is Home Assistant's own test harness for that one release. In the
# container of another release it would replace the Home Assistant installed
# there, so the script stops when the two do not match.
set -eu

tag="${1:?usage: scripts/run_ha_tests.sh <Home Assistant release>}"
case "$tag" in
  "" | *[!A-Za-z0-9._-]*) echo "unexpected tag: $tag" >&2; exit 2 ;;
esac
image="ghcr.io/home-assistant/home-assistant:$tag"
cd "$(dirname "$0")/.."

docker pull --quiet "$image" >/dev/null
digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)
echo "Image $image ${digest#*@}"
# the checkout is mounted read-only: the tests write to temporary directories
docker run --rm --entrypoint sh \
  -v "$PWD:/repo:ro" -w /repo \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$image" -c '
    set -eu
    version() { python3 -c "from homeassistant.const import __version__; print(__version__)"; }
    before=$(version)
    python3 -m pip install --quiet --disable-pip-version-check -r requirements_test_ha.txt 2>/dev/null \
      || uv pip install --system --quiet -r requirements_test_ha.txt
    after=$(version)
    if [ "$before" != "$after" ]; then
      echo "requirements_test_ha.txt brings Home Assistant $after, the container has $before:" >&2
      echo "run it with the release the plugin pins" >&2
      exit 2
    fi
    echo "Home Assistant $after, $(python3 -m pytest --version 2>&1)"
    # a deadlock between the event loop and the websocket thread is not
    # always ended by pytest-timeout: the whole run has 10 minutes
    status=0
    timeout 600 python3 -m pytest -p no:cacheprovider -ra --durations=8 tests_ha || status=$?
    if [ "$status" -eq 124 ] || [ "$status" -eq 143 ]; then
      echo "tests_ha did not finish in 10 minutes: something hangs" >&2
    fi
    exit "$status"
  '
