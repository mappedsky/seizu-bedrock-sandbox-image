# seizu-bedrock-sandbox-image

A container image that runs [Seizu](https://github.com/mappedsky/seizu)'s chat
sandbox on [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html).

Seizu's `bedrock` sandbox provider (`SANDBOX_PROVIDER=bedrock`) runs each
conversation's sandbox as one AgentCore Runtime session, and talks to the image
inside it over the **`seizu-sandbox/1`** protocol: one JSON operation per
`POST /invocations` request — run Python, run a shell command, read, write and
list files. This repository is one implementation of that protocol. The
protocol itself is defined in the Seizu repository
(`docs/root/dev/sandbox-image-protocol.md`); `conformance/` holds the copy of its
schema and checker this image is tested against.

## What is in the image

| | |
|---|---|
| Platforms | `linux/arm64` (what AgentCore Runtime runs) and `linux/amd64` (for running it locally) |
| Base | `python:3.13-slim-trixie`, pinned by digest |
| Python libraries | `pandas`, `numpy`, `scipy`, `matplotlib` (Agg backend), `requests`, `pyyaml`, `openpyxl`, `tabulate`, and their dependencies — wheels only, installed from `uv.lock` with their hashes checked |
| System packages | None beyond the base image |
| User | `1000:1000`, non-root |
| `$HOME` | `/home/user`, a symlink to the workspace mount `/mnt/workspace`, so everything Seizu writes there persists between turns |
| Server | `sandbox/server.py`, standard library only, on port 8080 |

Python runs in one persistent interpreter per session, as in a notebook:
variables survive between calls within a turn, a trailing expression's `repr()`
is returned, and top-level `await` works. Output written by subprocesses and C
extensions is captured along with `print`. When a session is stopped at the end
of a turn, its files persist and its interpreter does not.

## Using it with Seizu

AgentCore Runtime pulls images from Amazon ECR, so copy the `linux/arm64` image
into a repository in your account, and point Seizu at it **by digest**:

```bash
VERSION=0.1.0
ECR=123456789012.dkr.ecr.us-east-1.amazonaws.com/seizu-sandbox

aws ecr get-login-password | crane auth login --username AWS --password-stdin "${ECR%%/*}"
crane copy --platform linux/arm64 \
  "ghcr.io/mappedsky/seizu-bedrock-sandbox-image:${VERSION}" "${ECR}:${VERSION}"
crane digest "${ECR}:${VERSION}"
```

```
SANDBOX_PROVIDER=bedrock
SANDBOX_BEDROCK_IMAGE=123456789012.dkr.ecr.us-east-1.amazonaws.com/seizu-sandbox@sha256:...
```

The rest of the setup — execution role, network, storage mode, IAM — is in
Seizu's sandbox installation docs. Two things from there matter when choosing
an image:

- **With `session` storage, changing the image erases every conversation's
  files**, because changing it updates the runtime. Upgrade deliberately, or use
  `s3` storage.
- **Code in the sandbox can read the runtime execution role's credentials.**
  The image holds no credentials of its own; keep the role to what the runtime
  needs.

### Verifying an image

Each published image carries a signed build-provenance attestation, plus
BuildKit provenance and an SBOM:

```bash
gh attestation verify \
  oci://ghcr.io/mappedsky/seizu-bedrock-sandbox-image:0.1.0 \
  --repo mappedsky/seizu-bedrock-sandbox-image
```

### Tags

| Tag | Published from |
|---|---|
| `0.1.0`, `0.1`, `latest` | A `v0.1.0` release tag |
| `edge` | The latest commit on `main` |
| `sha-<commit>` | Every commit on `main`, and every release |

## Configuration

The image needs no configuration. These environment variables exist for
operators who manage the runtime themselves, and for tests:

| Variable | Default | What it controls |
|---|---|---|
| `SEIZU_SANDBOX_STORAGE_SEMANTICS` | detected | `posix` or `object`: what `hello` reports. Detected from the filesystem under `/mnt/workspace` (an S3 mount is `object`) |
| `SEIZU_SANDBOX_MAX_OUTPUT_BYTES` | `1048576` | Output kept per stream (stdout, stderr) per call; the rest is counted and dropped |
| `SEIZU_SANDBOX_MAX_FILE_BYTES` | `67108864` | Largest file `read_file` returns |
| `SEIZU_SANDBOX_MAX_REQUEST_BYTES` | `100663296` | Largest request body accepted |
| `SEIZU_SANDBOX_PORT` | `8080` | Listening port. AgentCore Runtime requires 8080 |

`SEIZU_SANDBOX_*` variables are removed from the environment sandboxed code
runs with.

## Security model

- **One session, one microVM.** AgentCore Runtime gives every session its own
  VM, so the sandboxed code and the server share a user without sharing
  anything with another conversation. Sandboxed code can stop its own server;
  that ends only its own session.
- **The server cannot be changed from inside the sandbox.** It runs as
  `python3 -I` from root-owned files, so a module, `.pth` file or `PYTHON*`
  variable the sandboxed code plants is not loaded by it.
- **Logs never carry data.** The server logs the operation, its outcome and its
  duration — never code, commands, file contents or output, since AgentCore
  sends container logs to CloudWatch. `scripts/check-image.sh` checks this
  against the built image.
- **Nothing secret or unique is generated at start-up.** AgentCore may restore
  one snapshot of a started container into every session, so the interpreter
  starts lazily on the first `run_python`.
- **Every input is pinned:** the base image and uv by digest, Python packages
  by hash, GitHub Actions and pre-commit hooks by commit. Dependencies resolve
  only to releases at least seven days old.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Development

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make test     # unit tests, including the Seizu conformance checker against an in-process server
make lint     # pre-commit: ruff, mypy, shellcheck, actionlint, zizmor
make check    # build the image for this machine and check it (conformance, persistence, logs)
```

`make check` runs the image with a fresh Docker volume at `/mnt/workspace`, runs
`conformance/sandbox_image_conformance.py` against it, then replaces the
container with a new one on the same volume and checks the workspace came back
and the interpreter did not — what a stopped AgentCore session does.

**Updating the protocol.** When Seizu changes the protocol or its checker, copy
them in from that Seizu commit and make the image pass:

```bash
scripts/sync-conformance.sh <seizu commit SHA>
make test check
```

**Adding a library.** Add it to the `sandbox` group in `pyproject.toml`, run
`uv lock`, and add it to the import check in the `Dockerfile`.

**Releasing.** Tag a commit on `main` as `v<major>.<minor>.<patch>`; the
`Publish` workflow builds and publishes it.

## License

Apache 2.0; see [LICENSE](LICENSE).
