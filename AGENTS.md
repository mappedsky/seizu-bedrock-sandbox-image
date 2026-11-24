# Agent Guidelines

This repository builds a `seizu-sandbox/1` image for Amazon Bedrock AgentCore
Runtime. The protocol belongs to Seizu (`docs/root/dev/sandbox-image-protocol.md`
and `reporting/services/sandbox_protocol.py` in mappedsky/seizu); this repository
implements it. A behaviour change the protocol does not describe is a change to
Seizu first.

## Layout

| Path | What it is |
|---|---|
| `sandbox/server.py` | The HTTP server and every protocol operation. Standard library only |
| `sandbox/kernel.py` | The persistent interpreter behind `run_python`, one process per session |
| `conformance/` | Vendored from Seizu at the commit in `conformance/SOURCE`. Never edit by hand; refresh with `scripts/sync-conformance.sh <sha>` |
| `scripts/check-image.sh` | Checks a built image: conformance, persistence across containers, log hygiene |
| `tests/` | Unit tests against an in-process server, including the vendored checker |

## Rules

- **The server stays standard-library only** and runs as `python3 -I`. The
  libraries sandboxed code uses are the `sandbox` dependency group; the server
  never imports them.
- **Never log request arguments, results, or exception messages derived from
  them.** Container logs go to CloudWatch. Log the op, outcome and duration.
- **Nothing secret or per-session at import or start-up** — the runtime may
  snapshot a started container.
- **Pin everything.** Actions by commit SHA with a `# vX.Y.Z` comment,
  pre-commit hooks by SHA with `# frozen: vX.Y.Z`, images by digest, Python
  packages through `uv.lock`. Pick releases at least seven days old, matching
  `exclude-newer` in `pyproject.toml`. No apt packages: they cannot be pinned the
  same way.
- **Workflows** start from `permissions: {}`, grant per job, and check out with
  `persist-credentials: false`. `zizmor` and `actionlint` run in pre-commit.

## Commands

```bash
make test     # uv run pytest
make lint     # pre-commit on every file
make check    # build for this machine, then scripts/check-image.sh
```

After editing `pyproject.toml`, run `uv lock` and commit both files together.
