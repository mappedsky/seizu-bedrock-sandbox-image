#!/usr/bin/env bash
# Check a built image the way AgentCore Runtime will use it: the Seizu
# conformance suite against a container with a fresh workspace volume, then
# that the workspace survives the container being replaced (a stopped session
# resuming), and that the server never logged the data it was sent.
#
#   scripts/check-image.sh IMAGE [--skip-timeouts]
set -euo pipefail

image="${1:?usage: scripts/check-image.sh IMAGE [--skip-timeouts]}"
shift
port="${PORT:-18080}"
url="http://127.0.0.1:${port}"
name="seizu-sandbox-check-$$"
volume="${name}-ws"
logs="$(mktemp)"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -f "$logs"
}
trap cleanup EXIT

user="$(docker image inspect --format '{{.Config.User}}' "$image")"
if [[ ! "$user" =~ ^[1-9][0-9]*(:[0-9]+)?$ ]]; then
  echo "FAIL  image USER must be a numeric non-root id, got '${user}'" >&2
  exit 1
fi
echo "ok    image runs as ${user}"

start() {
  docker run -d --name "$name" -p "127.0.0.1:${port}:8080" -v "${volume}:/mnt/workspace" "$image" >/dev/null
  for _ in $(seq 1 60); do
    if curl -fsS "${url}/ping" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  docker logs "$name" >&2
  echo "FAIL  image did not answer /ping" >&2
  exit 1
}

stop() {
  docker logs "$name" >>"$logs" 2>&1
  docker rm -f "$name" >/dev/null
}

# A new named volume takes the image's /mnt/workspace ownership, as the
# runtime's mount is writable by the image's user.
docker volume create "$volume" >/dev/null

start
python3 conformance/sandbox_image_conformance.py --url "$url" "$@"
python3 scripts/persistence_check.py write --url "$url"
stop

start
python3 scripts/persistence_check.py verify --url "$url"
stop

python3 scripts/persistence_check.py logs --log-file "$logs"
