#!/usr/bin/env bash
# Refresh conformance/ from a Seizu commit: the protocol's JSON Schema and the
# conformance checker, which together define what this image must do.
#
#   scripts/sync-conformance.sh <40-character seizu commit SHA>
set -euo pipefail

ref="${1:?usage: scripts/sync-conformance.sh <seizu commit SHA>}"
if [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pass a full commit SHA, not a branch or tag: the vendored copy must be reproducible" >&2
  exit 1
fi

base="https://raw.githubusercontent.com/mappedsky/seizu/${ref}"
dir="$(cd "$(dirname "$0")/.." && pwd)/conformance"

curl -fsSL --proto '=https' "${base}/scripts/sandbox_image_conformance.py" -o "${dir}/sandbox_image_conformance.py"
curl -fsSL --proto '=https' "${base}/schema/sandbox-protocol-v1.json" -o "${dir}/sandbox-protocol-v1.json"
chmod +x "${dir}/sandbox_image_conformance.py"

cat >"${dir}/SOURCE" <<SOURCE
repository: https://github.com/mappedsky/seizu
commit: ${ref}
sandbox_image_conformance.py: scripts/sandbox_image_conformance.py
sandbox-protocol-v1.json: schema/sandbox-protocol-v1.json
SOURCE

echo "conformance/ now matches mappedsky/seizu@${ref}; run 'make test check' before committing."
