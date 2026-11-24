#!/usr/bin/env python3
"""Check that a workspace outlives its container, as a stopped AgentCore session's does.

``write`` leaves a marker, a file and Python state in a running image; after the
container is replaced by a new one on the same volume, ``verify`` checks the
marker and file came back and the interpreter state did not. ``logs`` checks a
saved container log never carried the data the other steps sent.

Standard library only.
"""

import argparse
import base64
import json
import sys
import urllib.request
from typing import Any

PROTOCOL = "seizu-sandbox/1"
SENTINEL = "persistence-sentinel-9f2c"
MARKER = {
    "deployment": "persistence-check",
    "purpose": "persistence-check",
    "thread": "",
    "created_at": "2026-01-01T00:00:00+00:00",
}


def call(url: str, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({"protocol": PROTOCOL, "op": op, "args": args or {}, "timeout_seconds": 60}).encode()
    request = urllib.request.Request(f"{url}/invocations", data=body, method="POST")  # noqa: S310 -- caller-chosen URL
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 -- caller-chosen URL
        parsed: dict[str, Any] = json.loads(response.read())
    if parsed.get("ok") is not True:
        raise SystemExit(f"{op} failed: {parsed.get('error')!r}")
    result: dict[str, Any] = parsed["result"]
    return result


def write(url: str) -> None:
    call(url, "hello", {"create_marker": MARKER})
    content = base64.b64encode(SENTINEL.encode()).decode()
    call(url, "write_file", {"path": "/home/user/persist/kept.txt", "content_b64": content})
    call(url, "run_python", {"code": f"lost = {SENTINEL!r}\nprint(lost)"})


def verify(url: str) -> None:
    marker = call(url, "hello")["marker"]
    if marker != MARKER:
        raise SystemExit(f"marker did not survive a new container: {marker!r}")
    read = call(url, "read_file", {"path": "/home/user/persist/kept.txt"})
    if base64.b64decode(read["content_b64"]).decode() != SENTINEL:
        raise SystemExit("file did not survive a new container")
    state = call(url, "run_python", {"code": "lost"})
    if (state.get("error") or {}).get("name") != "NameError":
        raise SystemExit(f"interpreter state survived a new container: {state!r}")
    call(url, "wipe")


def logs(path: str) -> None:
    with open(path, encoding="utf-8", errors="replace") as handle:
        if SENTINEL in handle.read():
            raise SystemExit("the server logged request data")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["write", "verify", "logs"])
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--log-file", help="Container log to scan (the logs step).")
    args = parser.parse_args()
    if args.step == "write":
        write(args.url)
    elif args.step == "verify":
        verify(args.url)
    else:
        logs(args.log_file)
    print(f"ok    {args.step}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
